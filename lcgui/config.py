"""Read, validate and edit lancache's .env and docker-compose.yml, with backups and venue profiles."""
import ipaddress
import os
import re
import shutil
import time

from .util import COMPOSE_FILE, ENV_FILE, LANCACHE_DIR, data_path, host_ips, write_atomic

KEY_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")
FILES = {"env": ENV_FILE, "compose": COMPOSE_FILE}
PROFILE_RE = re.compile(r"^[A-Za-z0-9 _.-]{1,60}$")


def read_text(which):
    try:
        with open(FILES[which], encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


def parse_env(text=None):
    """Return [{key, value, help}] in file order. 'help' is the comment block just above the key."""
    text = read_text("env") if text is None else text
    out, comments = [], []
    for line in text.splitlines():
        s = line.strip()
        if not s:
            comments = []
            continue
        if s.startswith("#"):
            comments.append(s.lstrip("#").strip())
            continue
        m = KEY_RE.match(line)
        if m:
            val = m.group(2).strip()
            if len(val) >= 2 and val[0] == val[-1] and val[0] in "'\"":
                val = val[1:-1]
            out.append({"key": m.group(1), "value": val, "help": " ".join(c for c in comments if c)})
        comments = []
    return out


def env_dict():
    return {e["key"]: e["value"] for e in parse_env()}


def cache_root(env=None):
    env = env or env_dict()
    root = env.get("CACHE_ROOT", "./lancache") or "./lancache"
    return os.path.normpath(os.path.join(LANCACHE_DIR, root))


def logs_dir(env=None):
    return os.path.join(cache_root(env), "logs")


def _size_bytes(v):
    m = re.fullmatch(r"(\d+(?:\.\d+)?)([kmgt])", v.strip().lower())
    if not m:
        return None
    return float(m.group(1)) * 1024 ** "kmgt".index(m.group(2)) * 1024


def _existing_parent(path):
    while path and not os.path.exists(path):
        path = os.path.dirname(path)
    return path or "/"


def fs_usage(path):
    p = _existing_parent(path)
    st = os.statvfs(p)
    return {"path": p, "total": st.f_blocks * st.f_frsize, "free": st.f_bavail * st.f_frsize,
            "used": (st.f_blocks - st.f_bfree) * st.f_frsize}


def _ips(v, sep=None):
    parts = [p for p in re.split(sep or r"\s+", v.strip()) if p.strip()]
    for p in parts:
        ipaddress.IPv4Address(p.strip())
    return parts


def validate(env):
    """Return a list of {key, level: error|warn|info, msg}."""
    issues = []
    add = lambda k, lvl, msg: issues.append({"key": k, "level": lvl, "msg": msg})
    mine = {a["ip"] for a in host_ips()}

    for k in ("LANCACHE_IP", "DNS_BIND_IP"):
        if not env.get(k):
            add(k, "error", "Required.")
    try:
        ips = _ips(env.get("LANCACHE_IP", ""))
        missing = [i for i in ips if i not in mine]
        if missing:
            add("LANCACHE_IP", "warn", f"{', '.join(missing)} is not an address on this machine (this machine has {', '.join(sorted(mine)) or 'none'}).")
    except ValueError:
        add("LANCACHE_IP", "error", "Must be one or more IPv4 addresses separated by spaces.")
    try:
        ips = _ips(env.get("DNS_BIND_IP", ""))
        if len(ips) != 1:
            add("DNS_BIND_IP", "error", "Must be exactly one IPv4 address.")
        elif ips[0] not in mine:
            add("DNS_BIND_IP", "error", f"{ips[0]} is not on this machine, so the DNS container will fail to start. Use {', '.join(sorted(mine))}.")
    except ValueError:
        add("DNS_BIND_IP", "error", "Must be a single IPv4 address.")
    try:
        _ips(env.get("UPSTREAM_DNS", ""), r"[\s;]+")
    except ValueError:
        add("UPSTREAM_DNS", "error", "Must be IPv4 addresses separated by spaces or semicolons.")

    ugc = env.get("USE_GENERIC_CACHE", "")
    if ugc and ugc not in ("true", "false"):
        add("USE_GENERIC_CACHE", "error", "Must be true or false.")

    size = env.get("CACHE_DISK_SIZE", "")
    if not re.fullmatch(r"\d+[gt]", size):
        add("CACHE_DISK_SIZE", "error", "Must be a whole number with g or t, e.g. 2000g or 4t.")
    else:
        fs = fs_usage(cache_root(env))
        want = _size_bytes(size)
        if want > fs["total"]:
            add("CACHE_DISK_SIZE", "error", f"{size} is larger than the whole disk at {fs['path']} ({fs['total'] / 2**30:.0f} GiB). nginx will fail to manage the cache. Lower it or move CACHE_ROOT to a bigger disk.")
        elif want > fs["free"]:
            add("CACHE_DISK_SIZE", "warn", f"Only {fs['free'] / 2**30:.0f} GiB free at {fs['path']}; MIN_FREE_DISK will start pruning before the cache reaches {size}.")
        idx = env.get("CACHE_INDEX_SIZE", "")
        ib = _size_bytes(idx) if idx else None
        if ib:
            rec = want / 2 ** 40 * 250 * 2 ** 20
            if ib < rec * 0.9:
                add("CACHE_INDEX_SIZE", "warn", f"Recommended about {max(1, round(rec / 2**20))}m for a {size} cache (250m per TB).")
    if os.stat(_existing_parent(cache_root(env))).st_dev == os.stat("/").st_dev:
        add("CACHE_ROOT", "warn", f"The cache is on the system disk ({fs_usage('/')['total'] / 2**30:.0f} GiB). Use a dedicated drive if you can.")
    for k, rx, ex in (("MIN_FREE_DISK", r"\d+[mgt]", "10g"), ("CACHE_INDEX_SIZE", r"\d+[mg]", "500m"), ("CACHE_MAX_AGE", r"\d+d", "3650d")):
        if k in env and not re.fullmatch(rx, env[k]):
            add(k, "error", f"Wrong format, e.g. {ex}.")
    tz = env.get("TZ")
    if tz and not os.path.isfile(os.path.join("/usr/share/zoneinfo", tz)):
        add("TZ", "error", "Unknown timezone name (e.g. America/Chicago).")
    return issues


def backup(which):
    src = FILES[which]
    if not os.path.exists(src):
        return None
    name = f"{os.path.basename(src)}.{time.strftime('%Y%m%d-%H%M%S')}"
    dst = data_path("backups", name)
    shutil.copy2(src, dst)
    # Keep the newest 50 per file.
    d = os.path.dirname(dst)
    old = sorted(f for f in os.listdir(d) if f.startswith(os.path.basename(src) + "."))
    for f in old[:-50]:
        os.remove(os.path.join(d, f))
    return name


def list_backups():
    d = os.path.dirname(data_path("backups", "x"))
    out = []
    for f in sorted(os.listdir(d), reverse=True):
        p = os.path.join(d, f)
        out.append({"name": f, "file": "compose" if f.startswith("docker-compose") else "env",
                    "time": os.path.getmtime(p), "size": os.path.getsize(p)})
    return out


def read_backup(name):
    if name not in {b["name"] for b in list_backups()}:
        raise ValueError("bad name")
    with open(data_path("backups", name), encoding="utf-8") as f:
        return f.read()


def write_raw(which, text):
    if not text.endswith("\n"):
        text += "\n"
    b = backup(which)
    write_atomic(FILES[which], text)
    return b


def set_env(values):
    """Change/add keys in .env in place, keeping comments and order. A value of None removes the key."""
    for k, v in values.items():
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", k) or (v is not None and ("\n" in v or "\r" in v)):
            raise ValueError(f"Bad key or value for {k}")
    lines = read_text("env").splitlines()
    remaining = dict(values)
    out = []
    for line in lines:
        m = KEY_RE.match(line)
        if m and m.group(1) in remaining:
            v = remaining.pop(m.group(1))
            if v is None:
                continue
            out.append(f"{m.group(1)}={v}")
        else:
            out.append(line)
    added = [(k, v) for k, v in remaining.items() if v is not None]
    if added:
        out.append("")
        out.append("## Added from the web GUI")
        out += [f"{k}={v}" for k, v in added]
    return write_raw("env", "\n".join(out) + "\n")


# ---------- venue profiles: whole .env snapshots ----------
def _pdir():
    return os.path.dirname(data_path("profiles", "x"))


def list_profiles():
    out = []
    for f in sorted(os.listdir(_pdir())):
        if f.endswith(".env"):
            p = os.path.join(_pdir(), f)
            env = {e["key"]: e["value"] for e in parse_env(open(p, encoding="utf-8").read())}
            out.append({"name": f[:-4], "time": os.path.getmtime(p),
                        "summary": {k: env.get(k) for k in ("LANCACHE_IP", "DNS_BIND_IP", "UPSTREAM_DNS", "CACHE_ROOT", "CACHE_DISK_SIZE")}})
    return out


def save_profile(name):
    if not PROFILE_RE.fullmatch(name):
        raise ValueError("Use letters, numbers, spaces, dot, dash or underscore.")
    shutil.copy2(ENV_FILE, os.path.join(_pdir(), name + ".env"))


def load_profile(name):
    if not PROFILE_RE.fullmatch(name):
        raise ValueError("bad name")
    with open(os.path.join(_pdir(), name + ".env"), encoding="utf-8") as f:
        return write_raw("env", f.read())


def delete_profile(name):
    if not PROFILE_RE.fullmatch(name):
        raise ValueError("bad name")
    os.remove(os.path.join(_pdir(), name + ".env"))


# ---------- compose helpers ----------
def autostart_enabled():
    return 'restart-policy "unless-stopped"' in read_text("compose")


def set_autostart(on):
    txt = read_text("compose")
    a, b = 'x-restart-policy: &restart-policy "no"', 'x-restart-policy: &restart-policy "unless-stopped"'
    new = txt.replace(a, b) if on else txt.replace(b, a)
    if new == txt and (b in txt) != on:
        raise ValueError("Could not find the x-restart-policy line in docker-compose.yml")
    if new != txt:
        write_raw("compose", new)

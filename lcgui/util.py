"""Shared paths, settings and small helpers."""
import json
import os
import socket
import subprocess
import threading
import time

GUI_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LANCACHE_DIR = os.environ.get("LANCACHE_DIR", os.path.dirname(GUI_DIR))
DATA_DIR = os.environ.get("LCGUI_DATA", os.path.join(GUI_DIR, "data"))
STATIC_DIR = os.path.join(GUI_DIR, "static")
ENV_FILE = os.path.join(LANCACHE_DIR, ".env")
COMPOSE_FILE = os.path.join(LANCACHE_DIR, "docker-compose.yml")

os.makedirs(DATA_DIR, mode=0o700, exist_ok=True)


def data_path(*parts):
    p = os.path.join(DATA_DIR, *parts)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    return p


def write_atomic(path, text, mode=None):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    if mode is not None:
        os.chmod(tmp, mode)
    elif os.path.exists(path):
        os.chmod(tmp, os.stat(path).st_mode & 0o777)
    os.replace(tmp, path)


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save_json(path, obj, mode=0o600):
    write_atomic(path, json.dumps(obj, indent=2, sort_keys=True), mode)


def run(cmd, timeout=30, cwd=None):
    """Run a command, return (exit code, combined output). Never raises."""
    try:
        p = subprocess.run(cmd, cwd=cwd or LANCACHE_DIR, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout + p.stderr)
    except FileNotFoundError:
        return 127, f"{cmd[0]}: not installed"
    except subprocess.TimeoutExpired:
        return 124, "timed out"


def host_ips():
    """IPv4 addresses on this machine (not loopback / docker bridges), primary first."""
    out = []
    code, txt = run(["ip", "-j", "-4", "addr"], timeout=5)
    if code == 0:
        try:
            for itf in json.loads(txt):
                name = itf.get("ifname", "")
                if name == "lo" or name.startswith(("docker", "br-", "veth")):
                    continue
                for a in itf.get("addr_info", []):
                    out.append({"ip": a["local"], "prefix": a.get("prefixlen"), "iface": name,
                                "dynamic": bool(a.get("dynamic"))})
        except (ValueError, KeyError):
            pass
    # Put the address that holds the default route first.
    code, txt = run(["ip", "-4", "route", "get", "1.1.1.1"], timeout=5)
    if code == 0 and " src " in txt:
        src = txt.split(" src ")[1].split()[0]
        out.sort(key=lambda a: a["ip"] != src)
    return out


def primary_ip():
    ips = host_ips()
    return ips[0]["ip"] if ips else "127.0.0.1"


# ---------- GUI settings (separate from lancache's .env) ----------
DEFAULT_SETTINGS = {
    "active_window": 60,        # seconds without traffic before a client is no longer "downloading"
    "labels": {},               # ip -> friendly name, e.g. "Seat 12"
    "depot_overrides": {},      # steam depot id -> app id
    "event_name": "LAN Cache",
}
_settings_lock = threading.Lock()


def settings():
    s = dict(DEFAULT_SETTINGS)
    s.update(load_json(data_path("settings.json"), {}))
    return s


def update_settings(patch):
    with _settings_lock:
        s = settings()
        s.update(patch)
        save_json(data_path("settings.json"), s)
        return s


# ---------- audit log ----------
_audit_lock = threading.Lock()


def audit(user, action, detail=""):
    line = json.dumps({"t": time.time(), "user": user, "action": action, "detail": detail})
    with _audit_lock:
        with open(data_path("audit.log"), "a", encoding="utf-8") as f:
            f.write(line + "\n")


def audit_tail(n=300):
    try:
        with open(data_path("audit.log"), encoding="utf-8") as f:
            lines = f.readlines()[-n:]
    except OSError:
        return []
    out = []
    for ln in reversed(lines):
        try:
            out.append(json.loads(ln))
        except ValueError:
            pass
    return out


_rdns = {}
_rdns_lock = threading.Lock()


def reverse_dns(ip):
    """Cached, non-blocking hostname lookup: returns None until resolved in the background."""
    with _rdns_lock:
        if ip in _rdns:
            return _rdns[ip]
        _rdns[ip] = None

    def work():
        try:
            name = socket.gethostbyaddr(ip)[0]
        except OSError:
            name = ""
        with _rdns_lock:
            _rdns[ip] = name
    threading.Thread(target=work, daemon=True).start()
    return None

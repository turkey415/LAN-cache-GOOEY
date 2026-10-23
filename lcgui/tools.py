"""Pre-flight checks and a DNS tester that asks the lancache DNS server directly."""
import concurrent.futures
import os
import random
import socket
import struct

from . import config, docker, logs, system
from .util import host_ips, run

DNS_TESTS = [
    ("Steam", "lancache.steamcontent.com", True),
    ("Steam", "cache1-iad1.steamcontent.com", True),
    ("Epic Games", "epicgames-download1.akamaized.net", True),
    ("Battle.net", "level3.blizzard.com", True),
    ("Riot", "lol.secure.dyn.riotcdn.net", True),
    ("Windows Update", "dl.delivery.mp.microsoft.com", True),
    ("Not cached (upstream)", "www.google.com", False),
]


def dns_query(server, name, timeout=2.0):
    """Minimal A-record lookup. Returns list of IPs, or raises OSError."""
    tid = random.randint(0, 65535)
    q = struct.pack(">HHHHHH", tid, 0x0100, 1, 0, 0, 0)
    q += b"".join(bytes([len(p)]) + p.encode() for p in name.rstrip(".").split(".")) + b"\0" + struct.pack(">HH", 1, 1)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(timeout)
    try:
        s.sendto(q, (server, 53))
        data, _ = s.recvfrom(4096)
    finally:
        s.close()
    rid, flags, qd, an = struct.unpack(">HHHH", data[:8])
    if rid != tid:
        raise OSError("mismatched reply")
    if flags & 0xF:
        raise OSError({3: "name does not exist", 2: "server failure", 5: "refused"}.get(flags & 0xF, f"rcode {flags & 0xF}"))
    i = 12
    for _ in range(qd):
        while data[i]:
            i += data[i] + 1
        i += 5
    ips = []
    for _ in range(an):
        if data[i] & 0xC0 == 0xC0:
            i += 2
        else:
            while data[i]:
                i += data[i] + 1
            i += 1
        typ, _cls, _ttl, rdlen = struct.unpack(">HHIH", data[i:i + 10])
        i += 10
        if typ == 1 and rdlen == 4:
            ips.append(socket.inet_ntoa(data[i:i + 4]))
        i += rdlen
    return ips


def dns_check(extra=None):
    env = config.env_dict()
    server = env.get("DNS_BIND_IP", "")
    cache_ips = set(env.get("LANCACHE_IP", "").split())
    tests = list(DNS_TESTS)
    if extra:
        tests.append(("Custom", extra, None))
    def one(t):
        label, name, cached = t
        r = {"service": label, "name": name, "expect_cache": cached}
        try:
            ips = dns_query(server, name)
            r["ips"] = ips
            to_cache = bool(ips) and all(ip in cache_ips for ip in ips)
            r["to_cache"] = to_cache
            r["ok"] = (to_cache if cached else bool(ips) and not to_cache) if cached is not None else bool(ips)
        except (OSError, struct.error, IndexError) as e:
            r.update(ips=[], ok=False, error=str(e) or "no answer")
        return r
    with concurrent.futures.ThreadPoolExecutor(len(tests)) as ex:
        out = list(ex.map(one, tests))
    return {"server": server, "cache_ips": sorted(cache_ips), "results": out}


def _listening():
    code, out = run(["ss", "-H", "-ltnu"], timeout=5)
    res = []
    for line in out.splitlines():
        p = line.split()
        if len(p) >= 5:
            res.append((p[0], p[4]))
    return res


def preflight():
    """Checklist shown on the Server tab: [{id, ok: true|false|null(warn), title, detail}]."""
    env = config.env_dict()
    checks = []
    add = lambda cid, ok, title, detail="": checks.append({"id": cid, "ok": ok, "title": title, "detail": detail})

    d = docker.check()
    add("docker", d["usable"] and d["compose"], "Docker installed and usable",
        d["error"] or f"Docker {d.get('version')} · compose {d.get('compose_version')}")

    issues = config.validate(env)
    errs = [i for i in issues if i["level"] == "error"]
    warns = [i for i in issues if i["level"] == "warn"]
    add("env", False if errs else (None if warns else True), "Settings in .env are valid",
        "\n".join(f"{i['key']}: {i['msg']}" for i in errs + warns) or "No problems found")

    mine = [a["ip"] for a in host_ips()]
    add("ip", env.get("DNS_BIND_IP") in mine, "Cache IP matches this machine",
        f"DNS_BIND_IP={env.get('DNS_BIND_IP')} · LANCACHE_IP={env.get('LANCACHE_IP')} · this machine: {', '.join(mine)}")

    running = {c["service"] for c in docker.status() if c["state"] == "running"} if d["usable"] else set()
    listen = _listening()
    conflicts = []
    for port, proto, svc in (("53", "udp", "dns"), ("80", "tcp", "monolithic"), ("443", "tcp", "monolithic")):
        if svc in running:
            continue
        for p, addr in listen:
            host, _, prt = addr.rpartition(":")
            host = host.strip("[]").split("%")[0]
            if prt == port and p == proto and host in ("0.0.0.0", "*", "::", env.get("DNS_BIND_IP"), env.get("LANCACHE_IP", "").split(" ")[0]):
                conflicts.append(f"{proto}/{port} on {host}")
    add("ports", not conflicts, "Ports 53, 80, 443 free for lancache",
        ("In use by something else: " + ", ".join(conflicts)) if conflicts else "No conflicts")

    root = config.cache_root(env)
    fs = config.fs_usage(root)
    same = os.stat(fs["path"]).st_dev == os.stat("/").st_dev
    add("disk", None if same else True, "Cache storage",
        f"{root} → {fs['free'] / 2**30:.0f} GiB free of {fs['total'] / 2**30:.0f} GiB" + (" (system disk; a dedicated drive is better)" if same else ""))

    st = logs.STATS.follower.status
    add("logs", True if st == "following" else None, "Reading cache logs", st)

    ifs = [i for i in system.static_info()["ifaces"] if i["state"] == "up"]
    slow = [i for i in ifs if i["speed"] and i["speed"] < 10000]
    add("nic", None if slow else True, "Network link speed",
        ", ".join(f"{i['name']} {i['speed'] or '?'} Mb/s" for i in ifs) + (" · 1 Gb/s caps the whole cache at about 110 MB/s shared by all players" if slow else ""))

    add("autostart", None if not config.autostart_enabled() else True, "Cache starts automatically after reboot",
        "Enabled" if config.autostart_enabled() else "Off. Turn on once everything works.")
    code, out = run(["systemctl", "is-enabled", "lancache-gui"], timeout=5)
    add("gui-service", True if out.strip() == "enabled" else None, "Web GUI starts automatically after reboot",
        "Enabled" if out.strip() == "enabled" else "Not installed as a service. Run sudo ./webgui/install.sh")
    return checks

"""Delete one game's data from the cache.

nginx stores every cached slice as its own file and writes the request it came from ("KEY: steam/depot/731/chunk/…")
near the top of the file. A small script runs inside the cache container (as root there, so no sudo is needed),
reads that key from every file and deletes the ones that belong to the game. nginx treats a missing file as a miss,
so the next download of that game simply fetches it from the internet again.
"""
import json
import os
import re
import subprocess
import threading
import time

from . import games, logs, prefill
from .util import LANCACHE_DIR, audit

# Runs inside the monolithic container: python3 - '<rules json>'. Prints progress lines, then a DONE line.
SCRIPT = r'''
import json, os, re, sys
rules = [(i, re.compile(rx) if rx else None) for i, rx in json.loads(sys.argv[1])]
HEX = re.compile(r"^[0-9a-f]{32}$")
RANGE = re.compile(r"bytes=\d+-\d*$")
scanned = deleted = freed = 0
for root, dirs, files in os.walk("/data/cache/cache"):
    for f in files:
        if not HEX.match(f):           # temp files of downloads in progress
            continue
        p = os.path.join(root, f)
        scanned += 1
        try:
            with open(p, "rb") as fh:
                head = fh.read(4096)
            i = head.find(b"\nKEY: ")
            if i < 0:
                continue
            key = head[i + 6:head.index(b"\n", i + 6)].decode("utf-8", "replace")
            ident, sl, uri = key.partition("/")
            uri = RANGE.sub("", sl + uri)
            if any(ident == ri and (rx is None or rx.search(uri)) for ri, rx in rules):
                size = os.path.getsize(p)
                os.unlink(p)
                deleted += 1
                freed += size
        except (OSError, ValueError):
            continue
        if scanned % 5000 == 0:
            print("P", scanned, deleted, freed, flush=True)
print("DONE", scanned, deleted, freed, flush=True)
'''

_lock = threading.Lock()
_job = None


def _gkeys(gid):
    return [k for (k,) in logs.query("SELECT DISTINCT gkey FROM traffic") if games.describe(k)["id"] == gid]


def _steam_depots(appid, gkeys):
    """Every depot known to belong to a Steam app: seen in traffic, built in, set by hand, or looked up."""
    deps = {int(k.split("/")[1]) for k in gkeys if k.split("/")[1].isdigit()}
    deps |= {d for d, a in games.BUILTIN_DEPOTS.items() if a == appid}
    deps |= {int(d) for d, a in games.settings().get("depot_overrides", {}).items() if int(a) == appid}
    with games._lock:
        deps |= {d for d, a in games._depots.items() if a == appid}
    return sorted(d for d in deps if games.depot_app(d) == appid or f"steam/{d}" in gkeys)


def rules_for(gid):
    """[(cache identifier, uri regex or None)] matching every cache file of a game, plus a note of what it covers."""
    gk = _gkeys(gid)
    if gid.startswith("steam:depot:"):
        d = gid.rsplit(":", 1)[1]
        return [("steam", rf"^/depot/{d}/")], [int(d)], gk
    if gid == "steam:other":
        return [("steam", r"^/(?!depot/\d+/)")], [], gk
    if gid.startswith("steam:"):
        appid = int(gid.split(":")[1])
        deps = _steam_depots(appid, gk)
        if not deps:
            raise ValueError("No Steam depots are known for this game, so its files can't be found.")
        return [("steam", r"^/depot/(?:" + "|".join(map(str, deps)) + r")/")], deps, gk
    svc, _, rest = gid.partition("/")
    if svc == "epicgames" and rest != "-":
        return [("epicgames", r"^/Builds/(?:Org/[^/]+/)?" + re.escape(rest) + "/")], [], gk
    if svc == "blizzard" and rest != "-":
        return [("blizzard", r"^/tpr/" + re.escape(rest) + "/")], [], gk
    if not re.fullmatch(r"[A-Za-z0-9._-]+", svc):
        raise ValueError("Unknown game")
    # Other services only store the service name in the cache key, so the whole service goes.
    return [(svc, None)], [], gk


def whole_service(gid):
    svc, _, rest = gid.partition("/")
    return not gid.startswith("steam:") and not (svc in ("epicgames", "blizzard") and rest != "-")


def _forget_preload(appid, depots):
    """So the Preload tab doesn't think the deleted game is still in the cache."""
    if appid is None:
        return
    st = prefill._state()
    if str(appid) in st.get("builds", {}):
        st["builds"].pop(str(appid))
        prefill._save_state({"builds": st["builds"]})
    path = os.path.join(prefill.CONFIG_DIR, "successfullyDownloadedDepots.json")
    try:
        with open(path) as f:
            done = json.load(f)
        keep = {k: v for k, v in done.items() if int(k) not in depots}
        if keep != done:
            with open(path, "w") as f:
                json.dump(keep, f)
    except (OSError, ValueError):
        pass


def status():
    j = _job
    return dict(j) if j else None


def start(gid, user):
    global _job
    with _lock:
        if _job and _job["running"]:
            raise RuntimeError(f"Already deleting {_job['name']}. Wait for it to finish.")
        pj = prefill.current()
        if pj and pj.ended is None:
            raise RuntimeError("A preload is running. Stop it or let it finish first.")
        rules, depots, gkeys = rules_for(gid)
        name = games.describe(gkeys[0])["name"] if gkeys else gid
        appid = int(gid.split(":")[1]) if gid.startswith("steam:") and gid.split(":")[1].isdigit() else None
        _job = {"id": gid, "name": name, "running": True, "started": time.time(), "ended": None, "scanned": 0,
                "deleted": 0, "freed": 0, "error": None, "user": user}
        threading.Thread(target=_run, args=(_job, rules, depots, gkeys, appid), daemon=True, name="purge").start()
        return dict(_job)


def _run(j, rules, depots, gkeys, appid):
    try:
        p = subprocess.Popen(["docker", "compose", "exec", "-T", "monolithic", "python3", "-", json.dumps(rules)],
                             cwd=LANCACHE_DIR, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        p.stdin.write(SCRIPT)
        p.stdin.close()
        other = []
        for line in p.stdout:
            parts = line.split()
            if parts[:1] in (["P"], ["DONE"]) and len(parts) == 4:
                j.update(scanned=int(parts[1]), deleted=int(parts[2]), freed=int(parts[3]))
            elif line.strip():
                other.append(line.strip())
        code = p.wait()
        if code != 0:
            msg = other[-1] if other else f"exit {code}"
            if "not running" in msg or "no container" in msg.lower():
                msg = "The cache isn't running. Start it on the Service tab first."
            raise RuntimeError(msg)
        # The game's files are gone: drop its download history and the preload's record of it.
        if gkeys:
            c = logs.db()
            c.execute(f"DELETE FROM traffic WHERE gkey IN ({','.join('?' * len(gkeys))})", gkeys)
            c.commit()
            c.close()
        _forget_preload(appid, depots)
        audit(j["user"], "game-deleted", f"{j['name']} ({j['id']}): {j['deleted']} files, {j['freed']} bytes")
    except Exception as e:  # noqa: BLE001
        j["error"] = str(e)[:300]
        audit(j["user"], "game-delete-failed", f"{j['id']}: {j['error']}")
    finally:
        j.update(running=False, ended=time.time())

"""Cache storage: list disks, plan and apply disk changes through the root helper, then point .env at them."""
import hashlib
import json
import math
import os

from . import config, docker
from .util import GUI_DIR, run

HELPER_SRC = os.path.join(GUI_DIR, "tools", "lancache-gooey-disk")
HELPER_BIN = "/usr/local/sbin/lancache-gooey-disk"


def _src(args):
    """Run the helper from the repo as this user (read-only commands: scan, plan)."""
    code, out = run(["/usr/bin/python3", "-I", HELPER_SRC] + args, timeout=30)
    try:
        return json.loads(out.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {"error": out.strip()[-300:] or f"helper failed ({code})"}


def _sha(path):
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return None


def helper_state():
    installed = os.path.exists(HELPER_BIN)
    current = installed and _sha(HELPER_BIN) == _sha(HELPER_SRC)
    sudo_ok = False
    if installed:
        code, _ = run(["sudo", "-n", HELPER_BIN, "version"], timeout=10)
        sudo_ok = code == 0
    ok = installed and current and sudo_ok
    msg = None if ok else ("Disk changes need a one-time permission. Run: sudo ./webgui/install.sh" if not installed or not sudo_ok
                           else "The installed disk helper is out of date. Run: sudo ./webgui/install.sh")
    return {"installed": installed, "current": bool(current), "sudo": sudo_ok, "ok": ok, "message": msg}


def where_cache_is():
    root = config.cache_root()
    code, out = run(["findmnt", "-n", "-J", "-T", config._existing_parent(root), "-o", "TARGET,SOURCE,FSTYPE"], timeout=5)
    try:
        fm = json.loads(out)["filesystems"][0]
    except (ValueError, KeyError, IndexError):
        fm = {}
    return {"root": root, "mount": fm.get("target"), "source": fm.get("source"), "fstype": fm.get("fstype"),
            "fs": config.fs_usage(root), "on_system_disk": fm.get("target") == "/"}


def status():
    s = _src(["scan"])
    return {"scan": s, "helper": helper_state(), "cache": where_cache_is()}


def plan(action, devices, fs):
    return _src(["plan", action, "--fs", fs] + list(devices))


def sizes_for(total_bytes):
    """CACHE_DISK_SIZE (90% of the volume, leaving room for MIN_FREE_DISK) and CACHE_INDEX_SIZE (250m per TB)."""
    gib = total_bytes / 2 ** 30
    disk = max(1, int(gib * 0.9))
    index = max(500, int(math.ceil(gib / 1024 * 250 / 50.0) * 50))
    return {"CACHE_DISK_SIZE": f"{disk}g", "CACHE_INDEX_SIZE": f"{index}m"}


def apply(action, devices, fs, confirm, user):
    p = plan(action, devices, fs)
    if "error" in p:
        raise ValueError(p["error"])
    want = ",".join(sorted(p["erase"])) if p["erase"] else ",".join(devices)
    if ",".join(sorted(x for x in confirm.replace(" ", ",").split(",") if x)) != want:
        raise ValueError(f"Type exactly: {want.replace(',', ' ')}")
    h = helper_state()
    if not h["ok"]:
        raise ValueError(h["message"])
    was_running = any(c["service"] == "monolithic" and c["state"] == "running" for c in docker.status()) if docker.installed() else False

    def point_env(job):
        st = os.statvfs(p["mount"])
        vals = {"CACHE_ROOT": p["mount"], **sizes_for(st.f_blocks * st.f_frsize)}
        b = config.set_env(vals)
        job["out"].append("Updated .env (backup " + str(b) + "): " + ", ".join(f"{k}={v}" for k, v in vals.items()) + "\n")
        if not was_running:
            job["out"].append("The cache wasn't running. Press Start on the Server tab when ready.\n")
        return 0

    steps = [["sudo", "-n", HELPER_BIN, "apply", action, "--fs", p["fs"] if p["fs"] in ("ext4", "xfs") else "ext4", "--confirm", want] + list(devices),
             point_env]
    if was_running:
        steps.append(["docker", "compose", "up", "-d", "--force-recreate", "--remove-orphans"])
    return docker.start_job("storage-" + action, user, steps=steps)


def use_folder(path):
    """Use a folder on an already-mounted filesystem (no formatting)."""
    s = _src(["scan"])
    mps = {m for d in s.get("disks", []) if d["status"] == "mounted" for m in d["mountpoints"]}
    base = os.path.dirname(path.rstrip("/"))
    if base not in mps or os.path.basename(path.rstrip("/")) != "lancache":
        raise ValueError("Pick a mounted disk from the list")
    fs = config.fs_usage(base)
    vals = {"CACHE_ROOT": path, **sizes_for(fs["total"])}
    return config.set_env(vals), vals

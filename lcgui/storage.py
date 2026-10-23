"""Cache storage: list disks, plan and apply disk changes through the root helper, then point .env at them."""
import hashlib
import json
import math
import os

from . import config, docker
from .util import GUI_DIR, run

HELPER_SRC = os.path.join(GUI_DIR, "tools", "lancache-gooey-disk")
HELPER_BIN = "/usr/local/sbin/lancache-gooey-disk"


def _src(args, as_root=None):
    """Run a read-only helper command (scan, plan). Uses the installed helper as root when it is current,
    so it can read LVM details; otherwise the repo copy as this user."""
    if as_root is None:
        as_root = helper_state()["ok"]
    cmd = ["sudo", "-n", HELPER_BIN] if as_root else ["/usr/bin/python3", "-I", HELPER_SRC]
    code, out = run(cmd + args, timeout=30)
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
    h = helper_state()
    s = _src(["scan"], as_root=h["ok"])
    return {"scan": s, "helper": h, "cache": where_cache_is()}


def _plan_args(action, devices, fs, size, target):
    a = ["plan", action, "--fs", fs, "--size", str(int(size or 0))]
    if target:
        a += ["--target", target]
    return a + list(devices)


def plan(action, devices, fs, size=0, target=None):
    return _src(_plan_args(action, devices, fs, size, target))


def sizes_for(total_bytes):
    """CACHE_DISK_SIZE (90% of the volume, leaving room for MIN_FREE_DISK) and CACHE_INDEX_SIZE (250m per TB)."""
    gib = total_bytes / 2 ** 30
    disk = max(1, int(gib * 0.9))
    index = max(500, int(math.ceil(gib / 1024 * 250 / 50.0) * 50))
    return {"CACHE_DISK_SIZE": f"{disk}g", "CACHE_INDEX_SIZE": f"{index}m"}


def apply(action, devices, fs, confirm, user, size=0, target=None):
    h = helper_state()
    if not h["ok"]:
        raise ValueError(h["message"])
    p = plan(action, devices, fs, size, target)
    if "error" in p:
        raise ValueError(p["error"])
    want = p["confirm"]
    if ",".join(sorted(x for x in confirm.replace(" ", ",").split(",") if x)) != ",".join(sorted(want.split(","))):
        raise ValueError(f"Type exactly: {want.replace(',', ' ')}")
    was_running = any(c["service"] == "monolithic" and c["state"] == "running" for c in docker.status()) if docker.installed() else False

    def point_env(job):
        if not p.get("env_change", True):
            return 0
        st = os.statvfs(p["mount"])
        vals = {"CACHE_ROOT": p["mount"], **sizes_for(st.f_blocks * st.f_frsize)}
        b = config.set_env(vals)
        job["out"].append("Updated .env (backup " + str(b) + "): " + ", ".join(f"{k}={v}" for k, v in vals.items()) + "\n")
        if not was_running:
            job["out"].append("The cache wasn't running. Press Start on the Service tab when ready.\n")
        return 0

    args = _plan_args(action, devices, p["fs"] if p.get("fs") in ("ext4", "xfs") else "ext4", size, target)
    args[0] = "apply"
    steps = [["sudo", "-n", HELPER_BIN] + args[:-len(devices) or None] + ["--confirm", want] + list(devices), point_env]
    if was_running and p.get("env_change", True):
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

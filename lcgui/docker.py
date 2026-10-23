"""Control the lancache docker compose stack: status, start/stop/pull with live output, stats."""
import json
import shutil
import subprocess
import threading
import time
import uuid

from .util import LANCACHE_DIR, run

ACTIONS = {
    "start": [["compose", "up", "-d", "--remove-orphans"]],
    "stop": [["compose", "stop"]],
    "restart": [["compose", "up", "-d", "--force-recreate", "--remove-orphans"]],
    "down": [["compose", "down"]],
    "pull": [["compose", "pull"]],
    "update": [["compose", "pull"], ["compose", "up", "-d", "--remove-orphans"]],
    "validate": [["compose", "config", "-q"]],
}

_jobs = {}
_jobs_lock = threading.Lock()
_busy = threading.Lock()


def installed():
    return shutil.which("docker") is not None


def check():
    """{installed, usable, compose, version, error}"""
    if not installed():
        return {"installed": False, "usable": False, "compose": False,
                "error": "Docker is not installed. Run: sudo ./webgui/install.sh"}
    code, out = run(["docker", "version", "--format", "{{.Server.Version}}"], timeout=8)
    if code != 0:
        err = out.strip().splitlines()[-1] if out.strip() else "docker daemon not reachable"
        if "permission denied" in out:
            err = "This user can't use Docker. Run sudo ./webgui/install.sh (adds the user to the docker group), then restart the GUI."
        return {"installed": True, "usable": False, "compose": False, "error": err}
    c2, out2 = run(["docker", "compose", "version", "--short"], timeout=8)
    return {"installed": True, "usable": True, "version": out.strip(), "compose": c2 == 0,
            "compose_version": out2.strip() if c2 == 0 else None,
            "error": None if c2 == 0 else "docker compose plugin missing (apt install docker-compose-v2)"}


def status():
    """List of containers in the project with state/health."""
    code, out = run(["docker", "compose", "ps", "-a", "--format", "json"], timeout=15)
    if code != 0:
        return []
    out = out.strip()
    rows = []
    try:
        data = json.loads(out) if out.startswith("[") else [json.loads(l) for l in out.splitlines() if l.strip().startswith("{")]
    except ValueError:
        return []
    for c in data:
        rows.append({"service": c.get("Service"), "name": c.get("Name"), "state": c.get("State"),
                     "status": c.get("Status"), "health": c.get("Health") or "", "image": c.get("Image"),
                     "ports": c.get("Publishers") or c.get("Ports")})
    return rows


def stats():
    code, out = run(["docker", "stats", "--no-stream", "--format", "{{json .}}"], timeout=20)
    if code != 0:
        return []
    res = []
    for l in out.splitlines():
        try:
            d = json.loads(l)
            res.append({"name": d.get("Name"), "cpu": d.get("CPUPerc"), "mem": d.get("MemUsage"),
                        "net": d.get("NetIO"), "block": d.get("BlockIO"), "pids": d.get("PIDs")})
        except ValueError:
            pass
    return res


def images():
    code, out = run(["docker", "images", "--format", "{{json .}}", "lancachenet/*"], timeout=15)
    if code != 0:
        return []
    res = []
    for l in out.splitlines():
        try:
            d = json.loads(l)
            res.append({"repo": d.get("Repository"), "tag": d.get("Tag"), "created": d.get("CreatedSince"), "size": d.get("Size")})
        except ValueError:
            pass
    return res


def start_job(action, user):
    """Run an action in the background. Returns job id, or raises if another job is running."""
    if action not in ACTIONS:
        raise ValueError("unknown action")
    if not _busy.acquire(blocking=False):
        raise RuntimeError("Another server action is still running.")
    jid = uuid.uuid4().hex[:12]
    job = {"id": jid, "action": action, "user": user, "started": time.time(), "done": False, "code": None, "out": []}
    with _jobs_lock:
        _jobs[jid] = job
        for old in sorted(_jobs, key=lambda k: _jobs[k]["started"])[:-20]:
            _jobs.pop(old)

    def work():
        try:
            for args in ACTIONS[action]:
                job["out"].append(f"$ docker {' '.join(args)}\n")
                p = subprocess.Popen(["docker"] + args, cwd=LANCACHE_DIR, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, text=True, bufsize=1)
                for line in p.stdout:
                    job["out"].append(line)
                job["code"] = p.wait()
                if job["code"] != 0:
                    break
        except Exception as e:  # noqa: BLE001 - report anything to the UI
            job["out"].append(f"error: {e}\n")
            job["code"] = -1
        finally:
            job["done"] = True
            job["ended"] = time.time()
            _busy.release()
    threading.Thread(target=work, daemon=True).start()
    return jid


def job(jid, since=0):
    j = _jobs.get(jid)
    if not j:
        return None
    return {k: v for k, v in j.items() if k != "out"} | {"out": "".join(j["out"][since:]), "lines": len(j["out"])}


def recent_jobs():
    with _jobs_lock:
        return [{k: v for k, v in j.items() if k != "out"} for j in sorted(_jobs.values(), key=lambda j: -j["started"])]


def follow_logs(service, tail=200):
    """Popen streaming `docker compose logs -f` for a service. Caller must kill it."""
    return subprocess.Popen(["docker", "compose", "logs", "-f", "--no-color", "--tail", str(tail), service],
                            cwd=LANCACHE_DIR, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)

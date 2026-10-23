"""Checks GitHub for a newer version of this GUI and installs it.

The GUI folder is a git clone, so a check is just `git fetch` and an update is a fast-forward to the
fetched commit. Checks run shortly after start and then every 12 hours. With no internet (an offline LAN)
the check simply fails and says so; nothing else is affected.
"""
import glob
import os
import subprocess
import threading
import time

from .util import GUI_DIR

INTERVAL = 12 * 3600
FIRST_DELAY = 60
_lock = threading.Lock()
STATE = {"checked": None, "error": None, "behind": 0, "ahead": 0, "dirty": False, "commits": [],
         "current": None, "branch": None, "checking": False}


def _git(*args, timeout=30):
    """Run git in the GUI folder. Returns (exit code, output). Never asks for a password."""
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", LC_ALL="C")
    try:
        p = subprocess.run(["git", *args], cwd=GUI_DIR, capture_output=True, text=True, timeout=timeout, env=env)
        return p.returncode, (p.stdout + p.stderr).strip()
    except FileNotFoundError:
        return 127, "git is not installed"
    except subprocess.TimeoutExpired:
        return 124, "timed out"


def _current():
    code, out = _git("log", "-1", "--format=%h\t%ct\t%s")
    if code != 0:
        return None
    h, ct, subj = (out.split("\t", 2) + ["", ""])[:3]
    return {"hash": h, "time": int(ct or 0), "subject": subj}


def check():
    """Fetch from GitHub and work out whether an update is waiting. Returns the new state."""
    with _lock:
        STATE["checking"] = True
        try:
            if not os.path.isdir(os.path.join(GUI_DIR, ".git")):
                raise RuntimeError("This GUI was not installed with git clone, so it can't update itself.")
            code, branch = _git("rev-parse", "--abbrev-ref", "HEAD")
            code2, _ = _git("rev-parse", "--abbrev-ref", "@{u}")
            if code or code2:
                raise RuntimeError("This copy isn't following a GitHub branch.")
            code, out = _git("fetch", "--quiet", "origin", timeout=90)
            if code:
                raise RuntimeError("Couldn't reach GitHub (" + (out.splitlines() or ["no internet?"])[-1] + ")")
            _, counts = _git("rev-list", "--left-right", "--count", "HEAD...@{u}")
            ahead, behind = (int(x) for x in counts.split())
            _, log = _git("log", "-n", "30", "--format=%h\t%ct\t%s", "HEAD..@{u}")
            commits = [dict(zip(("hash", "time", "subject"), ln.split("\t", 2))) for ln in log.splitlines() if ln]
            for c in commits:
                c["time"] = int(c["time"])
            _, dirty = _git("status", "--porcelain", "--untracked-files=no")
            STATE.update(error=None, behind=behind, ahead=ahead, commits=commits, dirty=bool(dirty), branch=branch)
        except (RuntimeError, ValueError) as e:
            STATE["error"] = str(e)
        finally:
            STATE.update(checked=time.time(), current=_current(), checking=False)
        return dict(STATE)


def blocker():
    """Why an update can't be installed right now, or None."""
    if STATE["error"]:
        return STATE["error"]
    if not STATE["behind"]:
        return "Already up to date."
    if STATE["dirty"]:
        return "Files in the GUI folder were edited by hand on this machine. Commit or undo those edits first (git status shows them)."
    if STATE["ahead"]:
        return "This machine has commits that aren't on GitHub yet. Push them first, then update."
    return None


def apply():
    """Fast-forward to the GitHub version. Rolls back if the new code doesn't compile. Returns a summary line."""
    check()
    why = blocker()
    if why:
        raise ValueError(why)
    with _lock:
        before = _current()["hash"]
        code, out = _git("merge", "--ff-only", "@{u}", timeout=60)
        if code:
            raise ValueError("git couldn't update: " + out)
        files = [os.path.join(GUI_DIR, "server.py")] + glob.glob(os.path.join(GUI_DIR, "lcgui", "*.py"))
        try:
            for f in files:
                with open(f, encoding="utf-8") as fh:
                    compile(fh.read(), f, "exec")
        except (SyntaxError, ValueError, OSError) as e:
            _git("reset", "--hard", before)
            raise ValueError(f"The new version has an error, so it was rolled back: {e}")
        STATE.update(behind=0, commits=[], current=_current())
        return f"Updated {before} → {STATE['current']['hash']}"


def summary():
    s = dict(STATE)
    s["available"] = bool(s["behind"]) and not s["error"]
    s["next"] = (s["checked"] or time.time()) + INTERVAL
    return s


def start():
    def loop():
        time.sleep(FIRST_DELAY)
        while True:
            check()
            time.sleep(INTERVAL)
    STATE["current"] = _current()
    threading.Thread(target=loop, daemon=True, name="update-check").start()

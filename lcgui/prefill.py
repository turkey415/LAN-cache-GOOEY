"""Preload games into the cache with SteamPrefill (https://github.com/tpill90/steam-lancache-prefill).

SteamPrefill runs on this machine; it finds the cache through 'localhost' and downloads the selected
apps through it, so the next player gets them at LAN speed. It only downloads when Steam has a newer
build than the last successful preload, so pressing the button often is cheap.

SteamPrefill asks for a Steam login (and a Steam Guard code) on its first run. It runs in a pseudo
terminal here, and prompts are passed to the browser, so the login can be done from the web page.
"""
import json
import os
import platform
import pty
import re
import select
import shutil
import signal
import subprocess
import threading
import time
import urllib.request
import zipfile

from .util import audit, data_path, load_json, save_json, settings

TOOL_DIR = os.path.dirname(data_path("steamprefill", "x"))
BIN = os.path.join(TOOL_DIR, "SteamPrefill")
CONFIG_DIR = os.path.join(TOOL_DIR, "Config")
STATE_FILE = data_path("prefill_state.json")
LOG_FILE = data_path("prefill.log")      # full output of the latest preload run, for troubleshooting
RELEASES = "https://api.github.com/repos/tpill90/steam-lancache-prefill/releases/latest"
SIZE_RE = re.compile(r"Downloading ([\d.,]+) ?(B|KB|MB|GB|TB) from ([\d,]+) chunks")
START_RE = re.compile(r"Starting (.+?)\s*$")
UNITS = {"B": 1, "KB": 1e3, "MB": 1e6, "GB": 1e9, "TB": 1e12}   # SteamPrefill prints decimal units
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*(\x07|\x1b\\)|\x1b[()][A-Z0-9]|[\x00-\x08\x0b\x0c\x0e-\x1f]")
DEFAULT_APPS = [730]   # Counter-Strike 2

_lock = threading.Lock()
_job = None            # the running / last preload
_latest = {}           # appid -> {buildid, timeupdated, checked}


def _state():
    return load_json(STATE_FILE, {})


def _save_state(patch):
    s = _state()
    s.update(patch)
    save_json(STATE_FILE, s)


def apps():
    return [int(a) for a in settings().get("prefill_apps", DEFAULT_APPS)]


def installed():
    return os.path.isfile(BIN) and os.access(BIN, os.X_OK)


def version():
    return _state().get("tool_version")


def logged_in():
    return os.path.isfile(os.path.join(CONFIG_DIR, "account.config"))


def install(user="system"):
    """Download (or update) SteamPrefill for this CPU. Returns the version string."""
    arch = {"x86_64": "linux-x64", "amd64": "linux-x64", "aarch64": "linux-arm64", "arm64": "linux-arm64"}.get(platform.machine().lower())
    if not arch:
        raise RuntimeError(f"No SteamPrefill build for {platform.machine()}")
    req = urllib.request.Request(RELEASES, headers={"User-Agent": "lancache-gooey", "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        rel = json.load(r)
    asset = next((a for a in rel["assets"] if a["name"].endswith(arch + ".zip")), None)
    if not asset:
        raise RuntimeError(f"Release {rel.get('tag_name')} has no {arch} download")
    tmp = os.path.join(TOOL_DIR, "download.zip")
    with urllib.request.urlopen(urllib.request.Request(asset["browser_download_url"], headers={"User-Agent": "lancache-gooey"}), timeout=120) as r, open(tmp, "wb") as f:
        shutil.copyfileobj(r, f)
    with zipfile.ZipFile(tmp) as z:
        for m in z.infolist():
            # Extract only plain files inside TOOL_DIR, keeping our Config folder (the Steam login lives there).
            name = os.path.normpath(m.filename)
            if name.startswith("..") or os.path.isabs(name) or m.is_dir():
                continue
            parts = name.split(os.sep)
            if len(parts) > 1 and parts[0].lower().startswith("steamprefill"):
                name = os.path.join(*parts[1:])   # zips may wrap everything in one folder
            dest = os.path.join(TOOL_DIR, name)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with z.open(m) as src, open(dest, "wb") as out:
                shutil.copyfileobj(src, out)
    os.remove(tmp)
    os.chmod(BIN, 0o755)
    _save_state({"tool_version": rel.get("tag_name"), "tool_installed": time.time()})
    audit(user, "prefill-tool-installed", rel.get("tag_name"))
    return rel.get("tag_name")


def write_selection():
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(os.path.join(CONFIG_DIR, "selectedAppsToPrefill.json"), "w") as f:
        json.dump(apps(), f)


# ---------- latest build on Steam ----------
def check_latest():
    """Ask the Steam API for each app's current public build. Needs internet."""
    from . import games
    out = {}
    for a in apps():
        try:
            info = games._app_info(a) or {}
            pub = ((info.get("depots") or {}).get("branches") or {}).get("public") or {}
            out[a] = {"buildid": pub.get("buildid"), "timeupdated": int(pub["timeupdated"]) if pub.get("timeupdated") else None,
                      "name": (info.get("common") or {}).get("name") or games.app_name(a), "checked": time.time()}
        except Exception as e:  # noqa: BLE001 - offline
            out[a] = dict(_latest.get(a, {}), error=str(e)[:200], checked=time.time())
    _latest.update(out)
    return out


BIN_UNITS = {"B": 1, "KiB": 2 ** 10, "MiB": 2 ** 20, "GiB": 2 ** 30, "TiB": 2 ** 40, "KB": 1e3, "MB": 1e6, "GB": 1e9, "TB": 1e12}


def refresh_sizes():
    """Ask SteamPrefill for the full download size of each selected game ('select-apps status'), so the
    Cache Browser can show how complete a game is. Uses the saved Steam login; skipped while a preload runs."""
    if not (installed() and logged_in()) or (_job and _job.ended is None):
        return None
    write_selection()
    try:
        out = subprocess.run([BIN, "select-apps", "status", "--no-ansi"], cwd=TOOL_DIR, capture_output=True, text=True,
                             timeout=180, env=dict(os.environ, TERM="dumb", COLUMNS="140")).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    names = {}
    for a in apps():
        n = (_latest.get(a) or {}).get("name")
        if not n:
            from . import games
            n = games.app_name(a)
        if n:
            names[n.strip().lower()] = a
    sizes = {}
    for line in ANSI_RE.sub("", out).splitlines():
        cols = [c.strip() for c in line.split("│")]
        if len(cols) == 2 and cols[0] and cols[0].lower() in names:
            m = re.fullmatch(r"([\d.,]+)\s*([KMGT]?i?B)", cols[1])
            if m and m[2] in BIN_UNITS:
                sizes[str(names[cols[0].lower()])] = int(float(m[1].replace(",", "")) * BIN_UNITS[m[2]])
    if sizes:
        st = _state().get("sizes", {})
        st.update(sizes)
        _save_state({"sizes": st, "sizes_checked": time.time()})
    return sizes


def update_available():
    done = _state().get("builds", {})
    return [a for a, v in _latest.items() if v.get("buildid") and done.get(str(a)) != v.get("buildid")]


def status():
    s = _state()
    j = _job
    return {"installed": installed(), "version": s.get("tool_version"), "logged_in": logged_in(), "apps": apps(),
            "latest": {str(k): v for k, v in _latest.items()}, "builds": s.get("builds", {}), "last_run": s.get("last_run"),
            "update_available": update_available(), "auto": settings().get("prefill_auto", False),
            "sizes": s.get("sizes", {}),
            "os": settings().get("prefill_os", "windows"), "job": job_info(j) if j else None}


# ---------- running SteamPrefill in a pseudo terminal ----------
class Job:
    def __init__(self, args, user, auto=False, answers=None):
        self.args, self.user, self.auto = args, user, auto
        # Steam username/password typed into the GUI before the run. SteamPrefill gives up on its own
        # username and password prompts after 30 seconds, so these are answered at once. Kept in memory
        # only until used; never written anywhere.
        self.answers = dict(answers or {})
        self.stage = "starting"
        self.progress = {"total": 0, "done_before": 0, "base": None, "apps_done": 0, "app": None}
        self.started, self.ended, self.code = time.time(), None, None
        self.lines, self.partial, self.prompt, self.secret = [], "", None, False
        self.builds_at_start = {a: v.get("buildid") for a, v in _latest.items()}
        self.lock = threading.Lock()
        self.log = open(LOG_FILE, "w", encoding="utf-8", buffering=1)
        self.log.write(f"# {time.strftime('%Y-%m-%d %H:%M:%S')} SteamPrefill {' '.join(args)} (by {user})\n")
        pid, fd = pty.fork()
        if pid == 0:   # child
            os.chdir(TOOL_DIR)
            os.environ.update(TERM="dumb", COLUMNS="140", LINES="40", DOTNET_SYSTEM_GLOBALIZATION_INVARIANT="1")
            try:
                os.execv(BIN, [BIN] + args)
            finally:
                os._exit(127)
        self.pid, self.fd = pid, fd
        threading.Thread(target=self._read, daemon=True).start()

    def _add(self, text):
        parts = text.split("\n")
        with self.lock:
            parts[0] = self.partial + parts[0]
            for ln in parts[:-1]:
                ln = ln.split("\r")[-1].rstrip()   # progress bars redraw with \r: keep the final state
                if ln.strip():
                    self.lines.append(ln)
                    self._log(ln)
                    self._track(ln)
            self.partial = parts[-1]
            if len(self.lines) > 5000:
                del self.lines[:1000]

    def _track(self, ln):
        """Follow SteamPrefill's own messages to know the stage and the size of the current download."""
        from . import logs
        pr = self.progress
        low = ln.lower()
        m = SIZE_RE.search(ln)
        if m:
            pr["total"] = pr["done_before"] + float(m[1].replace(",", "")) * UNITS[m[2]]
            pr["base"] = logs.STATS.local_bytes
            self.stage = "downloading"
        elif "finished downloading" in low:
            if pr["base"] is not None:
                pr["done_before"] = pr["total"]
                pr["base"] = None
            pr["apps_done"] += 1
            self.stage = "finishing"
        elif START_RE.search(ln) and "login" not in low:
            pr["app"] = START_RE.search(ln)[1].strip()
            self.stage = "reading game info"
        elif "up to date" in low:
            self.stage = "checking"
        elif "logging" in low or "login" in low:
            self.stage = "logging in"
        elif "detecting lancache" in low or "detected lancache" in low:
            self.stage = "finding the cache"
        elif "prefill complete" in low:
            self.stage = "done"

    def progress_info(self):
        from . import logs
        pr = dict(self.progress)
        done = pr["done_before"] + (max(0, logs.STATS.local_bytes - pr["base"]) if pr["base"] is not None else 0)
        total = pr["total"]
        return {"stage": self.stage, "app": pr["app"], "total": total, "done": min(done, total) if total else done,
                "fraction": min(1.0, done / total) if total else None}

    def _log(self, line):
        try:
            self.log.write(time.strftime("%H:%M:%S ") + line + "\n")
        except (OSError, ValueError):
            pass

    def _read(self):
        last = time.time()
        while True:
            r, _, _ = select.select([self.fd], [], [], 0.5)
            if r:
                try:
                    data = os.read(self.fd, 65536)
                except OSError:
                    break
                if not data:
                    break
                self._add(ANSI_RE.sub("", data.decode("utf-8", "replace")))
                last = time.time()
                if last - getattr(self, "_plog", 0) > 10:   # progress redraws in place: note it now and then
                    self._plog = last
                    p = self.partial.split("\r")[-1].strip()
                    if p:
                        self._log("[progress] " + p[-200:])
                with self.lock:
                    self.prompt = None
            elif time.time() - last > 0.8:
                # Output stopped mid-line ending in ':' -> it's waiting for typed input.
                with self.lock:
                    p = self.partial.split("\r")[-1].strip()
                    auto_answer = None
                    if p.endswith(":") and self.prompt is None:
                        self.prompt = p
                        self.secret = "password" in p.lower()
                        self._log("[waiting for input] " + p)
                        low = p.lower()
                        if "account name" in low and self.answers.get("username"):
                            auto_answer = self.answers.pop("username")
                        elif "password" in low and "invalid" not in low and self.answers.get("password"):
                            auto_answer = self.answers.pop("password")
                if auto_answer is not None:
                    self.send(auto_answer)
                    last = time.time()
                    continue
                with self.lock:
                    stop_now = self.prompt is not None and self.auto
                    if stop_now:
                        self.lines.append("Automatic preload needs a Steam login. Run Preload by hand once to log in.")
                if stop_now:
                    threading.Thread(target=self.stop, daemon=True).start()
        _, st = os.waitpid(self.pid, 0)
        self.code = os.waitstatus_to_exitcode(st)
        with self.lock:
            if self.partial.strip():
                self.lines.append(self.partial.split("\r")[-1].rstrip())
                self._log(self.partial.split("\r")[-1].rstrip())
            self.partial, self.prompt = "", None
            self.answers.clear()
        self.ended = time.time()
        os.close(self.fd)
        self._log(f"[exit {self.code}]")
        self.log.close()
        tail = " ".join(self.lines[-20:]).lower()
        ok = self.code == 0 and not any(w in tail for w in ("unable to detect lancache", "exception", "error!", "failed"))
        st = _state()
        builds = st.get("builds", {})
        if ok:
            for a, b in self.builds_at_start.items():
                if b:
                    builds[str(a)] = b
        _save_state({"builds": builds, "last_run": {"t": self.ended, "ok": ok, "code": self.code, "user": self.user,
                                                     "force": "--force" in self.args, "auto": self.auto}})
        audit(self.user, "prefill-finished", f"exit {self.code}{' ok' if ok else ' FAILED'}")
        if ok:
            threading.Thread(target=refresh_sizes, daemon=True).start()

    def send(self, text):
        if self.ended:
            raise RuntimeError("Preload is not running")
        with self.lock:
            self.lines.append((self.prompt or "") + " " + ("•" * 8 if self.secret else text))
            self._log("[answered] " + (self.prompt or "") + " " + ("(hidden)" if self.secret else text))
            self.partial, self.prompt = "", None
        os.write(self.fd, text.encode() + b"\r")

    def stop(self):
        if not self.ended:
            with self.lock:
                self.lines.append("— stopped —")
            try:
                os.kill(self.pid, signal.SIGINT)
                time.sleep(1.5)
                if not self.ended:
                    os.kill(self.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def _phone_wait(j):
    """True while Steam waits for the sign-in to be approved in the Steam mobile app."""
    for ln in reversed(j.lines[-12:]):
        if "Use the Steam Mobile App to confirm" in ln:
            return True
        if any(w in ln.lower() for w in ("logged in", "detected lancache", "downloading", "up to date", "error", "failed")):
            return False
    return False


def job_info(j, since=0):
    with j.lock:
        return {"phone": j.ended is None and _phone_wait(j), "progress": j.progress_info(),"running": j.ended is None, "started": j.started, "ended": j.ended, "code": j.code, "auto": j.auto,
                "force": "--force" in j.args, "lines": j.lines[since:], "n": len(j.lines),
                "partial": j.partial.split("\r")[-1][-300:], "prompt": j.prompt, "secret": j.secret}


def start(user, force=False, auto=False, answers=None):
    global _job
    with _lock:
        if _job and _job.ended is None:
            raise RuntimeError("A preload is already running.")
        if not installed():
            raise RuntimeError("SteamPrefill isn't downloaded yet. Press 'Download SteamPrefill' first.")
        write_selection()
        if not _latest:
            check_latest()
        # --verbose makes SteamPrefill print the download size, which drives the progress bar.
        args = ["prefill", "--no-ansi", "--verbose", "--unit", "bits", "--os", settings().get("prefill_os", "windows")]
        if force:
            args.append("--force")
        _job = Job(args, user, auto, answers)
    audit(user, "prefill-started", ("forced " if force else "") + ("auto" if auto else ""))
    return job_info(_job)


def current():
    return _job


def watcher(cache_running):
    """Background: check Steam for new builds every 10 minutes; auto-preload when switched on."""
    def loop():
        time.sleep(20)
        while True:
            try:
                check_latest()
                if logged_in() and time.time() - _state().get("sizes_checked", 0) > 6 * 3600:
                    refresh_sizes()
                if (settings().get("prefill_auto") and update_available() and installed() and logged_in()
                        and not (_job and _job.ended is None) and cache_running()):
                    start("auto", auto=True)
            except Exception:  # noqa: BLE001
                pass
            time.sleep(600)
    threading.Thread(target=loop, daemon=True, name="prefill-watch").start()

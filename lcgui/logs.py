"""Follow the lancache nginx logs: live per-client stats, history in SQLite, and a feed for the log viewer.

The monolithic container logs each request like this (log_format "cachelog"):
[steam] 10.0.0.5 / - - - [23/Oct/2026:10:00:00 -0500] "GET /depot/2347770/chunk/ab... HTTP/1.1" 200 1048576 "-" "Valve/Steam HTTP Client 1.0" "HIT" "lancache.steamcontent.com" "-"
"""
import collections
import datetime
import os
import queue
import re
import sqlite3
import threading
import time

from . import config, games
from .util import data_path, is_local_ip

LINE_RE = re.compile(
    r'^\[(?P<svc>[^\]]*)\] (?P<ip>\S+) / \S+ - \S+ \[(?P<time>[^\]]+)\] "(?P<req>[^"]*)" (?P<status>\d{3}) (?P<bytes>\d+) '
    r'"[^"]*" "(?P<ua>[^"]*)" "(?P<cache>[^"]*)" "(?P<host>[^"]*)"')
CACHE_HIT = {"HIT", "STALE", "UPDATING", "REVALIDATED"}
CACHE_MISS = {"MISS", "EXPIRED", "BYPASS"}
LIVE_KEEP = 1800      # seconds of per-second detail kept in memory
DB = data_path("stats.sqlite3")


def logs_dir():
    return os.environ.get("LCGUI_LOGS_DIR") or config.logs_dir()


def db():
    c = sqlite3.connect(DB, timeout=15, check_same_thread=False)
    c.execute("PRAGMA journal_mode=WAL")
    return c


def _init_db():
    c = db()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS traffic(hour INTEGER, ip TEXT, gkey TEXT, hit INTEGER, miss INTEGER, other INTEGER,
        reqs INTEGER, first REAL, last REAL, PRIMARY KEY(hour, ip, gkey));
    CREATE INDEX IF NOT EXISTS traffic_gkey ON traffic(gkey);
    CREATE INDEX IF NOT EXISTS traffic_ip ON traffic(ip);
    CREATE TABLE IF NOT EXISTS state(k TEXT PRIMARY KEY, v TEXT);
    CREATE TABLE IF NOT EXISTS clients(ip TEXT PRIMARY KEY, ua TEXT, svc TEXT, last REAL);
    """)
    c.commit()
    c.close()


_init_db()


# ---------- publish/subscribe feed for the live log view ----------
class Hub:
    def __init__(self, keep=400):
        self.recent = collections.deque(maxlen=keep)
        self.subs = set()
        self.lock = threading.Lock()

    def publish(self, line):
        with self.lock:
            self.recent.append(line)
            for q in list(self.subs):
                try:
                    q.put_nowait(line)
                except queue.Full:
                    pass  # slow browser: drop lines rather than stall the reader

    def subscribe(self):
        q = queue.Queue(maxsize=5000)
        with self.lock:
            backlog = list(self.recent)
            self.subs.add(q)
        return q, backlog

    def unsubscribe(self, q):
        with self.lock:
            self.subs.discard(q)


HUBS = {"access": Hub(), "error": Hub()}


# ---------- file follower ----------
class Follower(threading.Thread):
    """Follow a log file across rotation.

    on_line(line) gets each complete line. on_progress(inode, offset, idle) is called after each chunk,
    once every line before `offset` has been handed to on_line. start_at() -> (inode, offset) resumes a saved spot;
    without it the follower starts near the end of the file (for the live view).
    """

    def __init__(self, name, on_line, start_at=None, on_progress=None):
        super().__init__(daemon=True, name=f"follow-{name}")
        self.fname, self.on_line, self.start_at, self.on_progress = name, on_line, start_at, on_progress
        self.status = "starting"
        self.path = None
        self.pos = 0
        self.ino = None

    def _emit(self, raw):
        self.on_line(raw.decode("utf-8", "replace").rstrip("\r"))

    def run(self):
        f = None
        buf = b""
        from_start = False
        while True:
            try:
                path = os.path.join(logs_dir(), self.fname)
                if f is None:
                    self.path = path
                    if not os.path.exists(path):
                        self.status = f"waiting for {path} (it appears once the cache server is running)"
                        time.sleep(2)
                        continue
                    f = open(path, "rb")
                    st = os.fstat(f.fileno())
                    self.ino = st.st_ino
                    if from_start:
                        self.pos = 0
                    elif self.start_at:
                        ino, pos = self.start_at()
                        self.pos = pos if (ino == st.st_ino and pos <= st.st_size) else 0
                    else:
                        self.pos = max(0, st.st_size - 64 * 1024)
                    f.seek(self.pos)
                    if self.pos and not self.start_at and not from_start:
                        self.pos += len(f.readline())   # skip a partial line
                    buf = b""
                    self.status = "following"
                chunk = f.read(1 << 20)
                if chunk:
                    buf += chunk
                    *lines, buf = buf.split(b"\n")
                    for ln in lines:
                        self._emit(ln)
                    self.pos = f.tell() - len(buf)
                    if self.on_progress:
                        self.on_progress(self.ino, self.pos, False)
                    continue
                if self.on_progress:
                    self.on_progress(self.ino, self.pos, True)
                time.sleep(0.5)
                if os.path.join(logs_dir(), self.fname) != self.path:   # CACHE_ROOT changed: follow the new folder
                    f.close()
                    f = None
                    from_start = True
                    continue
                try:
                    st = os.stat(path)
                except FileNotFoundError:
                    st = None
                if st is None or st.st_ino != self.ino or st.st_size < self.pos:
                    if st is None or st.st_ino != self.ino:
                        rest = buf + f.read()   # finish the rotated-away file first
                        for ln in rest.split(b"\n"):
                            if ln:
                                self._emit(ln)
                    f.close()
                    f = None
                    from_start = True   # a new or truncated file is read from its beginning
            except PermissionError:
                self.status = f"no permission to read {self.path}. Fix with: sudo chmod -R o+rX {logs_dir()}"
                f = None
                time.sleep(5)
            except OSError as e:
                self.status = f"error: {e}"
                f = None
                time.sleep(3)


# ---------- access log statistics ----------
_mon = {m: i for i, m in enumerate(["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1)}
_tcache = {}


def parse_time(s):
    # "23/Oct/2026:10:00:00 -0500"
    t = _tcache.get(s)
    if t is None:
        try:
            dt = datetime.datetime(int(s[7:11]), _mon[s[3:6]], int(s[0:2]), int(s[12:14]), int(s[15:17]), int(s[18:20]),
                                   tzinfo=datetime.timezone(datetime.timedelta(hours=int(s[22:24]), minutes=int(s[24:26])) * (-1 if s[21] == "-" else 1)))
            t = dt.timestamp()
        except (ValueError, KeyError, IndexError):
            t = time.time()
        if len(_tcache) > 5000:
            _tcache.clear()
        _tcache[s] = t
    return t


def classify(cache_status):
    if cache_status in CACHE_HIT:
        return 0
    if cache_status in CACHE_MISS:
        return 1
    return 2


class Stats:
    def __init__(self):
        self.lock = threading.Lock()
        self.live = {}                          # second -> {(ip, gkey): [hit, miss, other, reqs]}
        self.meta = {}                          # ip -> {ua, svc, last}
        self.pending = {}                       # (hour, ip, gkey) -> [hit, miss, other, reqs, first, last]
        self.last_flush = time.time()
        self.lines = 0
        self.bad = 0
        self.local_bytes = 0        # Steam bytes this machine (the preload) has pulled through the cache
        self.wdb = db()
        self.follower = Follower("access.log", self.on_line, start_at=self.saved_pos, on_progress=self.progress)
        self.err_follower = Follower("error.log", HUBS["error"].publish)
        self.last_flush = 0

    def start(self):
        self.follower.start()
        self.err_follower.start()

    def saved_pos(self):
        r = dict(self.wdb.execute("SELECT k, v FROM state WHERE k IN ('ino','pos')").fetchall())
        return (int(r["ino"]) if "ino" in r else None), int(r.get("pos", 0))

    def on_line(self, line):
        now = time.time()
        m = LINE_RE.match(line)
        if not m:
            if line.strip():
                self.bad += 1
                HUBS["access"].publish(line)
            return
        self.lines += 1
        ts = parse_time(m["time"])
        if ts > now - 120:
            HUBS["access"].publish(line)
        ip, n, cls = m["ip"], int(m["bytes"]), classify(m["cache"])
        if m["svc"] == "steam" and ts > now - 600 and is_local_ip(ip):
            self.local_bytes += n
        parts = m["req"].split(" ")
        path = parts[1] if len(parts) > 1 else ""
        gkey = games.identify(m["svc"], m["host"], path)
        sec = int(ts)
        hour = sec - sec % 3600
        with self.lock:
            if ts > now - LIVE_KEEP:
                v = self.live.setdefault(sec, {}).setdefault((ip, gkey), [0, 0, 0, 0])
                v[cls] += n
                v[3] += 1
            p = self.pending.setdefault((hour, ip, gkey), [0, 0, 0, 0, ts, ts])
            p[cls] += n
            p[3] += 1
            p[4] = min(p[4], ts)
            p[5] = max(p[5], ts)
            mt = self.meta.setdefault(ip, {})
            if ts >= mt.get("last", 0):
                mt.update(ua=m["ua"], svc=m["svc"], last=ts)

    def progress(self, ino, pos, idle):
        if time.time() - self.last_flush > (1 if idle else 5):
            self.flush(ino, pos)

    def flush(self, ino=None, pos=None):
        with self.lock:
            pend, self.pending = self.pending, {}
            meta = list(self.meta.items())
            cutoff = time.time() - LIVE_KEEP
            for sec in [s for s in self.live if s < cutoff]:
                del self.live[sec]
        self.last_flush = time.time()
        c = self.wdb
        if not pend and ino is not None and (str(ino), str(pos)) == getattr(self, "_saved", None):
            return
        self._saved = (str(ino), str(pos))
        c.executemany("""INSERT INTO traffic(hour, ip, gkey, hit, miss, other, reqs, first, last) VALUES(?,?,?,?,?,?,?,?,?)
            ON CONFLICT(hour, ip, gkey) DO UPDATE SET hit=hit+excluded.hit, miss=miss+excluded.miss, other=other+excluded.other,
            reqs=reqs+excluded.reqs, first=min(first, excluded.first), last=max(last, excluded.last)""",
                      [(h, ip, g, *v) for (h, ip, g), v in pend.items()])
        c.executemany("""INSERT INTO clients(ip, ua, svc, last) VALUES(?,?,?,?) ON CONFLICT(ip) DO UPDATE SET
            ua=excluded.ua, svc=excluded.svc, last=excluded.last WHERE excluded.last >= clients.last""",
                      [(ip, m.get("ua"), m.get("svc"), m.get("last", 0)) for ip, m in meta])
        if ino is not None:
            c.executemany("INSERT OR REPLACE INTO state(k, v) VALUES(?, ?)", [("ino", str(ino)), ("pos", str(pos))])
        c.commit()

    # ----- queries -----
    def active(self, window):
        """Clients with traffic in the last `window` seconds."""
        now = time.time()
        out = {}
        with self.lock:
            items = [(s, dict(b)) for s, b in self.live.items() if s >= now - window]
            meta = dict(self.meta)
        for sec, b in items:
            recent = sec >= now - 10
            for (ip, gkey), v in b.items():
                c = out.setdefault(ip, {"ip": ip, "hit": 0, "miss": 0, "other": 0, "reqs": 0, "rate_hit": 0, "rate_miss": 0,
                                        "games": {}, "last": 0, "first": sec})
                c["hit"] += v[0]
                c["miss"] += v[1]
                c["other"] += v[2]
                c["reqs"] += v[3]
                c["last"] = max(c["last"], sec)
                c["first"] = min(c["first"], sec)
                if recent:
                    c["rate_hit"] += v[0] / 10
                    c["rate_miss"] += v[1] / 10
                g = c["games"].setdefault(gkey, [0, 0])
                g[0] += v[0]
                g[1] += v[1]
        for ip, c in out.items():
            c.update(ua=meta.get(ip, {}).get("ua", ""), svc=meta.get(ip, {}).get("svc", ""))
        return list(out.values())

    def series(self, seconds=600, step=5):
        """Throughput history: [[t, hit bytes/s, miss bytes/s], ...] oldest first."""
        now = int(time.time())
        end = now - now % step
        start = end - seconds
        buckets = {t: [0, 0] for t in range(start, end + step, step)}
        with self.lock:
            items = [(s, list(b.values())) for s, b in self.live.items() if s >= start]
        for sec, b in items:
            t = sec - sec % step
            if t in buckets:
                for v in b:
                    buckets[t][0] += v[0] / step
                    buckets[t][1] += v[1] / step
        return [[t, *buckets[t]] for t in sorted(buckets)]


STATS = Stats()


def query(sql, args=()):
    c = db()
    try:
        return c.execute(sql, args).fetchall()
    finally:
        c.close()


def totals(since=0):
    r = query("SELECT COALESCE(SUM(hit),0), COALESCE(SUM(miss),0), COALESCE(SUM(other),0), COALESCE(SUM(reqs),0), COUNT(DISTINCT ip) FROM traffic WHERE hour >= ?",
              (since - since % 3600,))[0]
    return {"hit": r[0], "miss": r[1], "other": r[2], "reqs": r[3], "clients": r[4]}


def games_summary():
    rows = query("SELECT gkey, SUM(hit), SUM(miss), SUM(reqs), MIN(first), MAX(last) FROM traffic GROUP BY gkey")
    ips = query("SELECT DISTINCT gkey, ip FROM traffic")
    out = {}
    for gkey, hit, miss, reqs, first, last in rows:
        d = games.describe(gkey)
        g = out.setdefault(d["id"], {**d, "hit": 0, "miss": 0, "reqs": 0, "first": first, "last": last, "ips": set(), "depots": []})
        g["hit"] += hit
        g["miss"] += miss
        g["reqs"] += reqs
        g["first"] = min(g["first"], first)
        g["last"] = max(g["last"], last)
        if d["depot"]:
            g["depots"].append(d["depot"])
    for gkey, ip in ips:
        gid = games.describe(gkey)["id"]
        if gid in out:
            out[gid]["ips"].add(ip)
    res = []
    for g in out.values():
        g["clients"] = len(g.pop("ips"))
        g["depots"].sort()
        res.append(g)
    res.sort(key=lambda g: -(g["hit"] + g["miss"]))
    return res


def game_detail(gid):
    keys = [k for (k,) in query("SELECT DISTINCT gkey FROM traffic") if games.describe(k)["id"] == gid]
    if not keys:
        return None
    ph = ",".join("?" * len(keys))
    per_ip = query(f"SELECT ip, SUM(hit), SUM(miss), SUM(reqs), MIN(first), MAX(last) FROM traffic WHERE gkey IN ({ph}) GROUP BY ip ORDER BY MIN(first)", keys)
    hours = query(f"SELECT hour, SUM(hit), SUM(miss) FROM traffic WHERE gkey IN ({ph}) GROUP BY hour ORDER BY hour", keys)
    depots = query(f"SELECT gkey, SUM(hit), SUM(miss) FROM traffic WHERE gkey IN ({ph}) GROUP BY gkey ORDER BY SUM(miss) DESC", keys)
    d = games.describe(keys[0])
    return {**d, "clients": [{"ip": r[0], "hit": r[1], "miss": r[2], "reqs": r[3], "first": r[4], "last": r[5]} for r in per_ip],
            "hours": [list(r) for r in hours], "parts": [{"key": r[0], "hit": r[1], "miss": r[2]} for r in depots]}


def clients_summary():
    rows = query("SELECT ip, SUM(hit), SUM(miss), SUM(reqs), MIN(first), MAX(last) FROM traffic GROUP BY ip ORDER BY MAX(last) DESC")
    meta = {r[0]: r[1:] for r in query("SELECT ip, ua, svc FROM clients")}
    top = {}
    for ip, gkey, b in query("SELECT ip, gkey, SUM(hit)+SUM(miss) FROM traffic GROUP BY ip, gkey"):
        if b > top.get(ip, ("", -1))[1]:
            top[ip] = (gkey, b)
    return [{"ip": r[0], "hit": r[1], "miss": r[2], "reqs": r[3], "first": r[4], "last": r[5],
             "ua": meta.get(r[0], ("", ""))[0], "svc": meta.get(r[0], ("", ""))[1],
             "top": games.describe(top[r[0]][0])["name"] if r[0] in top else None} for r in rows]


def client_detail(ip):
    rows = query("SELECT gkey, SUM(hit), SUM(miss), SUM(reqs), MIN(first), MAX(last) FROM traffic WHERE ip=? GROUP BY gkey", (ip,))
    out = {}
    for gkey, hit, miss, reqs, first, last in rows:
        d = games.describe(gkey)
        g = out.setdefault(d["id"], {"id": d["id"], "name": d["name"], "service_name": d["service_name"], "appid": d["appid"],
                                     "hit": 0, "miss": 0, "reqs": 0, "first": first, "last": last})
        g["hit"] += hit
        g["miss"] += miss
        g["reqs"] += reqs
        g["first"] = min(g["first"], first)
        g["last"] = max(g["last"], last)
    return sorted(out.values(), key=lambda g: -g["last"])


def tail_file(name, n=300):
    path = os.path.join(logs_dir(), name)
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 256 * 1024))
            return f.read().decode("utf-8", "replace").splitlines()[-n:]
    except OSError:
        return []


def reset_stats():
    c = db()
    c.execute("DELETE FROM traffic")
    c.execute("DELETE FROM clients")
    c.commit()
    c.close()

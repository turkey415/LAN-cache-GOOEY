"""Host health: CPU, temperatures, memory, network, disks. Sampled every 2 s with 15 minutes of history."""
import collections
import glob
import os
import re
import threading
import time

from . import config
from .util import run

INTERVAL = 2
HISTORY = 450   # samples (15 minutes)


def _read(path, default=""):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return default


def _cpu_times():
    out = {}
    for line in _read("/proc/stat").splitlines():
        if line.startswith("cpu"):
            p = line.split()
            v = list(map(int, p[1:9]))
            idle = v[3] + v[4]
            out[p[0]] = (sum(v), idle)
    return out


def temps():
    """[{chip, label, c, high, crit}] for every hwmon temperature sensor."""
    res = []
    for hw in sorted(glob.glob("/sys/class/hwmon/hwmon*")):
        chip = _read(hw + "/name", "?")
        dev = os.path.basename(os.path.realpath(hw + "/device")) if os.path.exists(hw + "/device") else ""
        for inp in sorted(glob.glob(hw + "/temp*_input")):
            base = inp[:-6]
            try:
                c = int(_read(inp, "0")) / 1000
            except ValueError:
                continue
            if c <= 0:
                continue
            lbl = _read(base + "_label", "") or os.path.basename(base)
            hi = _read(base + "_max", "") or _read(base + "_crit", "")
            res.append({"chip": chip, "dev": dev, "label": lbl, "c": round(c, 1),
                        "high": int(hi) / 1000 if hi.lstrip("-").isdigit() and 0 < int(hi) < 150000 else None})
    return res


def cpu_temp(ts):
    for want in (("k10temp", "Tctl"), ("k10temp", "Tdie"), ("coretemp", "Package id 0"), ("zenpower", "Tdie")):
        for t in ts:
            if t["chip"] == want[0] and t["label"].startswith(want[1]):
                return t["c"]
    for t in ts:
        if t["chip"] in ("k10temp", "coretemp", "cpu_thermal", "zenpower"):
            return t["c"]
    return None


def cpu_freqs():
    f = []
    for p in glob.glob("/sys/devices/system/cpu/cpu[0-9]*/cpufreq/scaling_cur_freq"):
        v = _read(p)
        if v.isdigit():
            f.append(int(v) / 1000)
    mx = _read("/sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_max_freq")
    return {"avg": round(sum(f) / len(f)) if f else None, "max_now": round(max(f)) if f else None,
            "rated": round(int(mx) / 1000) if mx.isdigit() else None}


def meminfo():
    m = {}
    for line in _read("/proc/meminfo").splitlines():
        k, _, v = line.partition(":")
        m[k] = int(v.split()[0]) * 1024 if v.split() else 0
    return {"total": m.get("MemTotal", 0), "available": m.get("MemAvailable", 0), "cached": m.get("Cached", 0) + m.get("Buffers", 0),
            "swap_total": m.get("SwapTotal", 0), "swap_free": m.get("SwapFree", 0)}


def _net():
    out = {}
    for line in _read("/proc/net/dev").splitlines()[2:]:
        name, _, rest = line.partition(":")
        name = name.strip()
        if name == "lo" or name.startswith("veth"):
            continue
        v = rest.split()
        out[name] = (int(v[0]), int(v[8]))
    return out


def _disks():
    out = {}
    for line in _read("/proc/diskstats").splitlines():
        p = line.split()
        name = p[2]
        if re.fullmatch(r"(sd[a-z]+|nvme\d+n\d+|vd[a-z]+|md\d+|dm-\d+)", name):
            out[name] = (int(p[5]) * 512, int(p[9]) * 512, int(p[12]))  # read bytes, write bytes, ms busy
    return out


def disk_names():
    names = {}
    for d in glob.glob("/sys/block/*"):
        n = os.path.basename(d)
        model = _read(d + "/device/model") or _read(d + "/dm/name")
        size = _read(d + "/size", "0")
        names[n] = {"model": model, "size": int(size) * 512 if size.isdigit() else 0}
    return names


def link_speed(iface):
    v = _read(f"/sys/class/net/{iface}/speed")
    return int(v) if v.lstrip("-").isdigit() and int(v) > 0 else None


class Sampler(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True, name="sampler")
        self.hist = collections.deque(maxlen=HISTORY)
        self.now = {}
        self.lock = threading.Lock()
        self.cache_size = {"bytes": None, "at": 0, "error": None}

    def run(self):
        prev_cpu, prev_net, prev_disk, prev_t = _cpu_times(), _net(), _disks(), time.time()
        last_du = 0
        while True:
            time.sleep(INTERVAL)
            t = time.time()
            dt = max(t - prev_t, 0.001)
            cpu, net, disk = _cpu_times(), _net(), _disks()
            usage = {}
            for k, (tot, idle) in cpu.items():
                if k in prev_cpu:
                    dtot, didle = tot - prev_cpu[k][0], idle - prev_cpu[k][1]
                    usage[k] = round(100 * (1 - didle / dtot), 1) if dtot > 0 else 0
            nets = {k: {"rx": (v[0] - prev_net[k][0]) / dt, "tx": (v[1] - prev_net[k][1]) / dt}
                    for k, v in net.items() if k in prev_net}
            disks = {k: {"read": (v[0] - prev_disk[k][0]) / dt, "write": (v[1] - prev_disk[k][1]) / dt,
                         "util": min(100, (v[2] - prev_disk[k][2]) / dt / 10)} for k, v in disk.items() if k in prev_disk}
            ts = temps()
            snap = {"t": t, "cpu": usage.get("cpu", 0),
                    "cores": [usage[k] for k in sorted((k for k in usage if k != "cpu"), key=lambda k: int(k[3:]))],
                    "temp": cpu_temp(ts), "temps": ts, "freq": cpu_freqs(), "mem": meminfo(),
                    "load": [float(x) for x in _read("/proc/loadavg", "0 0 0").split()[:3]],
                    "net": nets, "disks": disks}
            with self.lock:
                self.now = snap
                self.hist.append({"t": t, "cpu": snap["cpu"], "temp": snap["temp"],
                                  "rx": sum(v["rx"] for k, v in nets.items() if not k.startswith(("docker", "br-"))),
                                  "tx": sum(v["tx"] for k, v in nets.items() if not k.startswith(("docker", "br-"))),
                                  "mem": 1 - snap["mem"]["available"] / max(1, snap["mem"]["total"])})
            prev_cpu, prev_net, prev_disk, prev_t = cpu, net, disk, t
            if t - last_du > (120 if self.cache_size.get("bytes") is None or self.cache_size.get("error") else 600):
                last_du = t
                threading.Thread(target=self._du, daemon=True).start()

    def _du(self):
        path = os.path.join(config.cache_root(), "cache")
        if not os.path.isdir(path):
            self.cache_size = {"bytes": 0, "at": time.time(), "error": "cache folder not created yet"}
            return
        # nginx keeps cache folders private to its own user, so ask inside the container first.
        code, out = run(["docker", "compose", "exec", "-T", "monolithic", "du", "-sb", "/data/cache"], timeout=600)
        if code == 0 and out.split() and out.split()[0].isdigit():
            self.cache_size = {"bytes": int(out.split()[0]), "at": time.time(), "error": None}
            return
        code, out = run(["du", "-sb", path], timeout=600)
        nbytes = int(out.split()[0]) if out.split() and out.split()[0].isdigit() else None
        err = None
        if code != 0:
            err = "some cache folders are not readable by the GUI user; size is a lower bound" if nbytes else out.strip()[-200:]
        self.cache_size = {"bytes": nbytes, "at": time.time(), "error": err}

    def snapshot(self):
        with self.lock:
            return dict(self.now), list(self.hist)


SAMPLER = Sampler()


def uptime():
    try:
        return float(_read("/proc/uptime").split()[0])
    except (ValueError, IndexError):
        return 0


def static_info():
    model = ""
    for line in _read("/proc/cpuinfo").splitlines():
        if line.startswith("model name"):
            model = line.split(":", 1)[1].strip()
            break
    ifaces = []
    for d in sorted(glob.glob("/sys/class/net/*")):
        n = os.path.basename(d)
        if n == "lo" or n.startswith(("veth", "docker", "br-")):
            continue
        ifaces.append({"name": n, "speed": link_speed(n), "state": _read(d + "/operstate"), "mac": _read(d + "/address")})
    return {"cpu_model": model, "cores": os.cpu_count(), "hostname": os.uname().nodename, "kernel": os.uname().release,
            "ifaces": ifaces, "disks": disk_names()}

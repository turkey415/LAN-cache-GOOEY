"""Work out which game a cache request belongs to, look up names, and fetch cover art."""
import json
import os
import queue
import re
import threading
import time
import urllib.request

from .util import data_path, load_json, save_json, settings

# Known Steam depots, so CS2 is recognized with no internet. (From the app 730 depot list.)
BUILTIN_DEPOTS = {d: 730 for d in list(range(731, 739)) + list(range(2347770, 2347800))}
BUILTIN_DEPOTS.update({d: 228980 for d in range(228980, 229010)})
BUILTIN_APPS = {730: "Counter-Strike 2", 228980: "Steamworks Common Redistributables"}

SERVICE_NAMES = {
    "steam": "Steam", "epicgames": "Epic Games", "blizzard": "Battle.net", "riot": "Riot Games",
    "origin": "EA / Origin", "uplay": "Ubisoft Connect", "wsus": "Windows Update", "xboxlive": "Xbox",
    "nintendo": "Nintendo", "sony": "PlayStation", "rockstar": "Rockstar", "wargaming": "Wargaming",
    "arenanet": "ArenaNet", "daybreak": "Daybreak", "frontier": "Frontier", "neverwinter": "Neverwinter",
    "pathofexile": "Path of Exile", "warframe": "Warframe", "teso": "Elder Scrolls Online", "cod": "Call of Duty",
    "bsg": "Battlestate", "hirez": "Hi-Rez", "renegadex": "Renegade X", "test": "Test",
}
BLIZZARD = {"wow": "World of Warcraft", "wow_classic": "WoW Classic", "pro": "Overwatch 2", "d3": "Diablo III", "fenris": "Diablo IV",
            "hs": "Hearthstone", "hsb": "Hearthstone", "s2": "StarCraft II", "s1": "StarCraft", "hero": "Heroes of the Storm",
            "w3": "Warcraft III", "viper": "Call of Duty: Black Ops 4", "odin": "Call of Duty: MW", "auks": "Call of Duty",
            "zeus": "Call of Duty: Black Ops Cold War", "lazr": "Call of Duty: MW2 Campaign", "agent": "Battle.net Agent", "bna": "Battle.net App",
            "anbs": "Diablo Immortal", "wlby": "Crash Bandicoot 4", "rtro": "Blizzard Arcade Collection", "gryphon": "Warcraft Rumble"}

DEPOT_RE = re.compile(r"^/depot/(\d+)/")
EPIC_RE = re.compile(r"^/Builds/(?:Org/[^/]+/)?([^/]+)/")
TPR_RE = re.compile(r"^/tpr/([^/]+)/")

_lock = threading.Lock()
_depots = {int(k): v for k, v in load_json(data_path("depots.json"), {}).items()}   # depot -> app id or 0 (unknown)
_apps = {int(k): v for k, v in load_json(data_path("apps.json"), {}).items()}       # app id -> name
_tried = {}
_q = queue.Queue()
_sizes = load_json(data_path("app_sizes.json"), {})   # app id -> {"bytes": full Windows download, "t": when checked}


def identify(svc, host, path):
    """Return a compact game key for a request: 'steam/<depot>', 'epic/<name>', 'blizzard/<code>', or '<svc>/<host>'."""
    if svc == "steam":
        m = DEPOT_RE.match(path)
        return f"steam/{m.group(1)}" if m else "steam/-"
    if svc == "epicgames":
        m = EPIC_RE.match(path)
        return f"epicgames/{m.group(1)}" if m else "epicgames/-"
    if svc == "blizzard":
        m = TPR_RE.match(path)
        return f"blizzard/{m.group(1)}" if m else "blizzard/-"
    if svc == "riot":
        h = host.split(".")[0]
        return f"riot/{h}"
    return f"{svc}/{host or '-'}"


def depot_app(depot):
    ov = settings().get("depot_overrides", {})
    if str(depot) in ov:
        return int(ov[str(depot)])
    if depot in BUILTIN_DEPOTS:
        return BUILTIN_DEPOTS[depot]
    with _lock:
        app = _depots.get(depot)
    if app is None and _tried.get(depot, 0) < time.time() - 3600:
        _tried[depot] = time.time()
        _q.put(depot)
    return app or None


def app_name(appid):
    if appid in BUILTIN_APPS:
        return BUILTIN_APPS[appid]
    with _lock:
        n = _apps.get(appid)
    if n is None and _tried.get(("app", appid), 0) < time.time() - 3600:
        _tried[("app", appid)] = time.time()
        _q.put(("app", appid))
    return n


def _windows_download_size(info):
    """Full Windows download of an app from Steam's depot list: depots for Windows (or any OS), no language
    packs, no optional DLC, not borrowed from another app (shared redistributables). Matches SteamPrefill."""
    total = 0
    for k, v in (info.get("depots") or {}).items():
        if not k.isdigit() or not isinstance(v, dict) or v.get("depotfromapp") or v.get("dlcappid"):
            continue
        cfg = v.get("config") or {}
        if cfg.get("optionaldlc") or cfg.get("language") or (cfg.get("oslist") and "windows" not in cfg["oslist"]):
            continue
        man = (v.get("manifests") or {}).get("public") or {}
        try:
            total += int(man.get("download") or 0)
        except (TypeError, ValueError):
            pass
    return total or None


def full_size(appid):
    """Estimated full Windows download size of a Steam app (bytes), or None while unknown. Refreshed daily."""
    v = _sizes.get(str(appid))
    if (not v or time.time() - v["t"] > 86400) and _tried.get(("size", appid), 0) < time.time() - 3600:
        _tried[("size", appid)] = time.time()
        _q.put(("size", appid))
    return v["bytes"] if v else None


def describe(key):
    """Map a game key to the thing shown in the UI: {id, service, service_name, name, appid, depot}."""
    svc, _, rest = key.partition("/")
    d = {"service": svc, "service_name": SERVICE_NAMES.get(svc, svc.title()), "appid": None, "depot": None}
    if svc == "steam":
        if rest.isdigit():
            depot = int(rest)
            d["depot"] = depot
            app = depot_app(depot)
            if app:
                d.update(id=f"steam:{app}", appid=app, name=app_name(app) or f"Steam app {app}")
            else:
                d.update(id=f"steam:depot:{depot}", name=f"Steam depot {depot} (identifying…)")
        else:
            d.update(id="steam:other", name="Steam (client / manifests)")
    elif svc == "epicgames":
        d.update(id=key, name="Epic Games: " + (re.sub(r"(?<=[a-z])(?=[A-Z])", " ", rest) if rest != "-" else "launcher"))
    elif svc == "blizzard":
        d.update(id=key, name=BLIZZARD.get(rest, f"Battle.net: {rest}"))
    else:
        d.update(id=key, name=f"{d['service_name']}: {rest}" if rest != "-" else d["service_name"])
    return d


def _get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "lancache-gui/1.0"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.load(r)


def _app_info(appid):
    d = _get_json(f"https://api.steamcmd.net/v1/info/{appid}")
    return (d.get("data") or {}).get(str(appid))


def _resolve_depot(depot):
    # Most depots belong to an app with a slightly smaller id; check a few candidates.
    for cand in [depot - 1, depot, depot - 2, depot - 3, depot - 4, depot - 5, depot - 10]:
        if cand <= 0:
            continue
        try:
            info = _app_info(cand)
        except Exception:  # noqa: BLE001 - offline or rate limited; try again later
            return None
        time.sleep(0.5)
        if info and str(depot) in (info.get("depots") or {}):
            name = (info.get("common") or {}).get("name")
            with _lock:
                _depots[depot] = cand
                if name:
                    _apps[cand] = name
            return cand
    with _lock:
        _depots[depot] = 0
    return 0


def _worker():
    while True:
        item = _q.get()
        try:
            if isinstance(item, tuple) and item[0] == "size":
                info = _app_info(item[1])
                size = info and _windows_download_size(info)
                if size:
                    with _lock:
                        _sizes[str(item[1])] = {"bytes": size, "t": time.time()}
                        save_json(data_path("app_sizes.json"), _sizes)
                continue
            if isinstance(item, tuple):
                info = _app_info(item[1])
                name = info and (info.get("common") or {}).get("name")
                if name:
                    with _lock:
                        _apps[item[1]] = name
            else:
                _resolve_depot(item)
            with _lock:
                save_json(data_path("depots.json"), _depots)
                save_json(data_path("apps.json"), _apps)
        except Exception:  # noqa: BLE001
            pass


threading.Thread(target=_worker, daemon=True, name="depot-resolver").start()


# ---------- cover art ----------
COVER_URLS = [
    "https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/{a}/header.jpg",
    "https://cdn.cloudflare.steamstatic.com/steam/apps/{a}/header.jpg",
    "https://steamcdn-a.akamaihd.net/steam/apps/{a}/header.jpg",
]


def cover(appid):
    """Return (bytes, content type) for a Steam app's header image, cached on disk; None if unavailable."""
    path = data_path("covers", f"{int(appid)}.jpg")
    miss = path + ".miss"
    if os.path.exists(path):
        with open(path, "rb") as f:
            return f.read(), "image/jpeg"
    if os.path.exists(miss) and time.time() - os.path.getmtime(miss) < 6 * 3600:
        return None
    for u in COVER_URLS:
        try:
            req = urllib.request.Request(u.format(a=int(appid)), headers={"User-Agent": "lancache-gui/1.0"})
            with urllib.request.urlopen(req, timeout=8) as r:
                if r.status == 200 and r.headers.get_content_type().startswith("image/"):
                    data = r.read()
                    with open(path, "wb") as f:
                        f.write(data)
                    return data, "image/jpeg"
        except Exception:  # noqa: BLE001
            continue
    open(miss, "w").close()
    return None

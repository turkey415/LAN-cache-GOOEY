#!/usr/bin/env python3
"""LAN Cache GOOEY: web control panel for a lancache.net docker stack.

Python standard library only. Serves HTTPS on all addresses (default port 8443).

  python3 server.py                     run it
  python3 server.py --reset-password admin
"""
import argparse
import http.server
import json
import mimetypes
import os
import re
import socket
import socketserver
import ssl
import sys
import threading
import time
import urllib.parse

from lcgui import auth, config, docker, games, logs, prefill, storage, system, tools
from lcgui.util import (DATA_DIR, LANCACHE_DIR, STATIC_DIR, audit, audit_tail, data_path, host_ips, is_local_ip, primary_ip,
                        reverse_dns, run, settings, update_settings)

VERSION = "1.0"
COOKIE = "lcdeck"
MAX_BODY = 2 * 1024 * 1024


class HttpError(Exception):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code, self.msg = code, msg


# ---------------------------------------------------------------- routing
ROUTES = []


def route(method, pattern, role="viewer"):
    """role: None = no login needed, 'viewer' = any logged in user, 'admin' = admins only."""
    rx = re.compile("^" + pattern + "$")

    def deco(fn):
        ROUTES.append((method, rx, role, fn))
        return fn
    return deco


def ua_platform(ua):
    u = ua.lower()
    for k, v in (("valve/steam", "Steam client"), ("epicgameslauncher", "Epic Games Launcher"), ("battle.net", "Battle.net"),
                 ("blizzard", "Battle.net"), ("riot", "Riot Client"), ("origin", "EA app"), ("ubisoft", "Ubisoft Connect"),
                 ("uplay", "Ubisoft Connect"), ("microsoft-delivery-optimization", "Windows Update"), ("windows-update", "Windows Update"),
                 ("xbox", "Xbox"), ("nintendo", "Nintendo"), ("playstation", "PlayStation")):
        if k in u:
            return v
    return ua[:40] if ua else ""


# ---------------------------------------------------------------- auth endpoints
@route("POST", "/api/login", role=None)
def r_login(h, body):
    ip = h.client_address[0]
    if auth.rate_limited(ip):
        raise HttpError(429, "Too many failed logins. Wait a few minutes.")
    tok = auth.login(str(body.get("username", "")), str(body.get("password", "")))
    if not tok:
        auth.note_failure(ip)
        audit(body.get("username", "?"), "login-failed", ip)
        raise HttpError(401, "Wrong username or password.")
    audit(body["username"], "login", ip)
    h.set_cookie = f"{COOKIE}={tok}; Path=/; HttpOnly; Secure; SameSite=Strict; Max-Age={auth.SESSION_TTL}"
    return {"ok": True}


@route("POST", "/api/logout")
def r_logout(h, body):
    auth.logout(h.token)
    h.set_cookie = f"{COOKIE}=; Path=/; HttpOnly; Secure; SameSite=Strict; Max-Age=0"
    return {"ok": True}


@route("GET", "/api/me")
def r_me(h, q):
    s = h.sess
    return {"user": s["user"], "role": s["role"], "csrf": s["csrf"], "version": VERSION,
            "hostname": socket.gethostname(), "ip": primary_ip(), "event": settings().get("event_name")}


@route("POST", "/api/me/password")
def r_my_password(h, body):
    if not auth.check_password(h.sess["user"], str(body.get("current", ""))):
        raise HttpError(400, "Current password is wrong.")
    pw = str(body.get("new", ""))
    if len(pw) < 8:
        raise HttpError(400, "Use at least 8 characters.")
    auth.set_user(h.sess["user"], password=pw)
    audit(h.sess["user"], "password-changed")
    try:
        os.remove(data_path("INITIAL_ADMIN_PASSWORD.txt"))
    except OSError:
        pass
    return {"ok": True, "relogin": True}


# ---------------------------------------------------------------- users
@route("GET", "/api/users", role="admin")
def r_users(h, q):
    return {"users": auth.list_users(), "audit": audit_tail(300)}


@route("POST", "/api/users", role="admin")
def r_user_save(h, body):
    name, role, pw = str(body.get("name", "")).strip(), body.get("role"), body.get("password")
    if not re.fullmatch(r"[A-Za-z0-9_.-]{2,32}", name):
        raise HttpError(400, "Username: 2-32 letters, numbers, dot, dash or underscore.")
    if role not in auth.ROLES:
        raise HttpError(400, "Role must be admin or viewer.")
    exists = any(u["name"] == name for u in auth.list_users())
    if not exists and not pw:
        raise HttpError(400, "A new user needs a password.")
    if pw is not None and pw != "" and len(pw) < 8:
        raise HttpError(400, "Use at least 8 characters.")
    if exists and role != "admin" and auth.admin_count() <= 1 and any(u["name"] == name and u["role"] == "admin" for u in auth.list_users()):
        raise HttpError(400, "Can't remove the last admin.")
    auth.set_user(name, password=pw or None, role=role)
    audit(h.sess["user"], "user-saved", f"{name} ({role}){' password set' if pw else ''}")
    return {"ok": True}


@route("DELETE", "/api/users/(?P<name>[^/]+)", role="admin")
def r_user_del(h, q, name):
    if name == h.sess["user"]:
        raise HttpError(400, "You can't delete yourself.")
    if any(u["name"] == name and u["role"] == "admin" for u in auth.list_users()) and auth.admin_count() <= 1:
        raise HttpError(400, "Can't delete the last admin.")
    auth.delete_user(name)
    audit(h.sess["user"], "user-deleted", name)
    return {"ok": True}


# ---------------------------------------------------------------- dashboard data
def _is_local(ip):
    return is_local_ip(ip)


def _label(ip):
    return settings()["labels"].get(ip) or ("Preload (this machine)" if _is_local(ip) else None)


@route("GET", "/api/overview")
def r_overview(h, q):
    now = time.time()
    lt = time.localtime(now)
    midnight = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
    active = logs.STATS.active(settings()["active_window"])
    snap, _ = system.SAMPLER.snapshot()
    return {"all": logs.totals(0), "today": logs.totals(midnight), "active": len(active),
            "rate_hit": sum(c["rate_hit"] for c in active), "rate_miss": sum(c["rate_miss"] for c in active),
            "series": logs.STATS.series(int(q.get("seconds", 600)), 5),
            "containers": docker.status() if docker.installed() else [], "docker": docker.installed(),
            "cpu": snap.get("cpu"), "temp": snap.get("temp"), "log_status": logs.STATS.follower.status,
            "lines": logs.STATS.lines, "net": snap.get("net", {}),
            "prefill": {"update": prefill.update_available(), "running": bool(prefill.current() and prefill.current().ended is None)}}


@route("GET", "/api/clients/active")
def r_active(h, q):
    win = int(q.get("window", settings()["active_window"]))
    out = []
    for c in logs.STATS.active(win):
        tot = c["hit"] + c["miss"]
        frac = c["hit"] / tot if tot else None
        gl = []
        for gkey, (hit, miss) in sorted(c["games"].items(), key=lambda kv: -(kv[1][0] + kv[1][1])):
            d = games.describe(gkey)
            if not any(x["id"] == d["id"] for x in gl):
                gl.append({"id": d["id"], "name": d["name"], "appid": d["appid"], "service": d["service_name"], "hit": hit, "miss": miss})
            else:
                x = next(x for x in gl if x["id"] == d["id"])
                x["hit"] += hit
                x["miss"] += miss
        c.update(games=gl, hit_frac=frac, label=_label(c["ip"]), hostname=reverse_dns(c["ip"]), platform=ua_platform(c["ua"]),
                 source="cache" if frac is not None and frac >= .9 else "internet" if frac is not None and frac <= .1 else "mixed" if frac is not None else "idle")
        out.append(c)
    out.sort(key=lambda c: -(c["rate_hit"] + c["rate_miss"]))
    return {"window": win, "clients": out}


@route("GET", "/api/clients")
def r_clients(h, q):
    rows = logs.clients_summary()
    for r in rows:
        r.update(label=_label(r["ip"]), hostname=reverse_dns(r["ip"]), platform=ua_platform(r["ua"] or ""))
    return {"clients": rows}


@route("GET", "/api/clients/(?P<ip>[0-9a-fA-F.:]+)")
def r_client(h, q, ip):
    return {"ip": ip, "label": _label(ip), "hostname": reverse_dns(ip), "games": logs.client_detail(ip)}


@route("GET", "/api/games")
def r_games(h, q):
    cs = system.SAMPLER.cache_size
    pst = prefill.status()
    return {"games": logs.games_summary(), "disk": cs, "fs": config.fs_usage(config.cache_root()),
            "limit": config.env_dict().get("CACHE_DISK_SIZE"),
            "preloaded": {"builds": pst["builds"], "latest": {k: v.get("buildid") for k, v in pst["latest"].items()}, "sizes": pst["sizes"],
                          "apps": pst["apps"], "last_run": pst["last_run"]}}


@route("GET", "/api/games/(?P<gid>[A-Za-z0-9:_./-]+)")
def r_game(h, q, gid):
    g = logs.game_detail(gid)
    if not g:
        raise HttpError(404, "Unknown game")
    for c in g["clients"]:
        c.update(label=_label(c["ip"]), hostname=reverse_dns(c["ip"]))
    return g


@route("GET", "/api/cover/(?P<appid>\\d+)")
def r_cover(h, q, appid):
    res = games.cover(int(appid))
    if not res:
        raise HttpError(404, "no cover")
    h.send_raw(200, res[0], res[1], cache=True)


@route("POST", "/api/labels", role="admin")
def r_labels(h, body):
    labels = settings()["labels"]
    ip, name = str(body.get("ip", "")), str(body.get("label", "")).strip()[:40]
    if not re.fullmatch(r"[0-9a-fA-F.:]{2,45}", ip):
        raise HttpError(400, "bad ip")
    if name:
        labels[ip] = name
    else:
        labels.pop(ip, None)
    update_settings({"labels": labels})
    return {"ok": True}


@route("POST", "/api/depots", role="admin")
def r_depot(h, body):
    ov = settings()["depot_overrides"]
    depot, app = str(body.get("depot", "")), str(body.get("appid", ""))
    if not depot.isdigit():
        raise HttpError(400, "bad depot")
    if app.isdigit():
        ov[depot] = int(app)
    else:
        ov.pop(depot, None)
    update_settings({"depot_overrides": ov})
    audit(h.sess["user"], "depot-mapped", f"{depot} -> {app or 'auto'}")
    return {"ok": True}


@route("POST", "/api/stats/reset", role="admin")
def r_stats_reset(h, body):
    logs.reset_stats()
    audit(h.sess["user"], "stats-reset")
    return {"ok": True}


@route("GET", "/api/system")
def r_system(h, q):
    snap, hist = system.SAMPLER.snapshot()
    return {"now": snap, "hist": hist, "info": system.static_info(), "uptime": system.uptime(),
            "cache_size": system.SAMPLER.cache_size, "fs": config.fs_usage(config.cache_root()),
            "rootfs": config.fs_usage("/"),
            "containers": docker.stats() if q.get("containers") and docker.installed() else None}


# ---------------------------------------------------------------- logs
@route("GET", "/api/logs/stream")
def r_log_stream(h, q):
    src = q.get("source", "access")
    h.start_sse()
    if src in logs.HUBS:
        hub = logs.HUBS[src]
        qu, backlog = hub.subscribe()
        if not backlog:
            backlog = logs.tail_file(f"{src}.log", 200)
        try:
            h.sse({"lines": backlog[-300:], "status": logs.STATS.follower.status if src == "access" else logs.STATS.err_follower.status})
            while True:
                batch = []
                try:
                    batch.append(qu.get(timeout=15))
                    while len(batch) < 500:
                        batch.append(qu.get_nowait())
                except Exception:  # noqa: BLE001 - queue.Empty
                    pass
                h.sse({"lines": batch} if batch else None)
                time.sleep(0.25)
        finally:
            hub.unsubscribe(qu)
    elif src in ("dns", "monolithic", "gui"):
        if src == "gui":
            p = None
            import subprocess
            p = subprocess.Popen(["journalctl", "-u", "lancache-gui", "-f", "-n", "200", "-o", "short-iso"],
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
        else:
            if not docker.installed():
                h.sse({"lines": ["Docker is not installed yet."]})
                return
            p = docker.follow_logs(src)
        lines, lock, done = [], threading.Lock(), threading.Event()

        def pump():
            for ln in p.stdout:
                with lock:
                    lines.append(ln.rstrip("\n"))
            done.set()
        threading.Thread(target=pump, daemon=True).start()
        try:
            while True:
                time.sleep(0.3)
                with lock:
                    batch, lines[:] = lines[:], []
                if batch:
                    h.sse({"lines": batch[-2000:]})
                elif done.is_set():
                    h.sse({"lines": ["— log stream ended —"], "ended": True})
                    return
                else:
                    h.sse_ping()
        finally:
            p.kill()
    else:
        raise HttpError(400, "unknown source")


# ---------------------------------------------------------------- config
@route("GET", "/api/config")
def r_config(h, q):
    entries = config.parse_env()
    env = {e["key"]: e["value"] for e in entries}
    return {"env": entries, "issues": config.validate(env), "raw": {"env": config.read_text("env"), "compose": config.read_text("compose")},
            "paths": {"env": config.ENV_FILE, "compose": config.COMPOSE_FILE, "lancache": LANCACHE_DIR, "cache_root": config.cache_root(env),
                      "data": DATA_DIR},
            "host_ips": host_ips(), "profiles": config.list_profiles(), "backups": config.list_backups(),
            "autostart": config.autostart_enabled(), "settings": settings(), "fs": config.fs_usage(config.cache_root(env))}


@route("POST", "/api/config/validate")
def r_config_validate(h, body):
    env = config.env_dict()
    env.update({k: v for k, v in (body.get("values") or {}).items() if v is not None})
    return {"issues": config.validate(env)}


@route("POST", "/api/config/env", role="admin")
def r_config_env(h, body):
    values = {str(k): (None if v is None else str(v).strip()) for k, v in (body.get("values") or {}).items()}
    try:
        b = config.set_env(values)
    except ValueError as e:
        raise HttpError(400, str(e))
    audit(h.sess["user"], "env-changed", ", ".join(f"{k}={v}" for k, v in values.items()))
    return {"ok": True, "backup": b, "issues": config.validate(config.env_dict())}


@route("POST", "/api/config/raw", role="admin")
def r_config_raw(h, body):
    which = body.get("file")
    if which not in config.FILES:
        raise HttpError(400, "bad file")
    b = config.write_raw(which, str(body.get("content", "")))
    audit(h.sess["user"], "file-edited", which)
    check = None
    if docker.check()["usable"]:
        code, out = run(["docker", "compose", "config", "-q"], timeout=20)
        check = {"ok": code == 0, "out": out.strip()}
    return {"ok": True, "backup": b, "compose_check": check}


@route("GET", "/api/config/backup")
def r_backup(h, q):
    try:
        return {"name": q.get("name"), "content": config.read_backup(q.get("name", ""))}
    except (OSError, ValueError):
        raise HttpError(404, "not found")


@route("POST", "/api/config/restore", role="admin")
def r_restore(h, body):
    name = str(body.get("name", ""))
    try:
        content = config.read_backup(name)
    except (OSError, ValueError):
        raise HttpError(404, "not found")
    which = "compose" if name.startswith("docker-compose") else "env"
    config.write_raw(which, content)
    audit(h.sess["user"], "backup-restored", name)
    return {"ok": True}


@route("POST", "/api/config/profile", role="admin")
def r_profile(h, body):
    act, name = body.get("action"), str(body.get("name", "")).strip()
    try:
        if act == "save":
            config.save_profile(name)
        elif act == "load":
            config.load_profile(name)
        elif act == "delete":
            config.delete_profile(name)
        else:
            raise HttpError(400, "bad action")
    except (OSError, ValueError) as e:
        raise HttpError(400, str(e))
    audit(h.sess["user"], f"profile-{act}", name)
    return {"ok": True}


@route("POST", "/api/settings", role="admin")
def r_settings(h, body):
    patch = {}
    if "active_window" in body:
        patch["active_window"] = max(10, min(900, int(body["active_window"])))
    if "event_name" in body:
        patch["event_name"] = str(body["event_name"])[:60]
    update_settings(patch)
    audit(h.sess["user"], "gui-settings", json.dumps(patch))
    return {"ok": True}


@route("POST", "/api/cert/regenerate", role="admin")
def r_cert(h, body):
    make_cert(force=True)
    audit(h.sess["user"], "cert-regenerated")
    return {"ok": True, "note": "Restart the GUI to use the new certificate."}


# ---------------------------------------------------------------- server control
@route("GET", "/api/server")
def r_server(h, q):
    d = docker.check()
    res = {"docker": d, "jobs": docker.recent_jobs(), "autostart": config.autostart_enabled()}
    if d["usable"]:
        res.update(containers=docker.status(), images=docker.images())
    if q.get("preflight"):
        res["preflight"] = tools.preflight()
    return res


@route("POST", "/api/server/action", role="admin")
def r_server_action(h, body):
    act = body.get("action")
    if act in ("start", "restart", "update"):
        errs = [i for i in config.validate(config.env_dict()) if i["level"] == "error"]
        if errs and not body.get("force"):
            raise HttpError(409, "Fix these settings first (or start anyway): " + "; ".join(f"{i['key']}: {i['msg']}" for i in errs))
    if not docker.check()["usable"]:
        raise HttpError(409, docker.check()["error"])
    try:
        jid = docker.start_job(act, h.sess["user"])
    except (ValueError, RuntimeError) as e:
        raise HttpError(409, str(e))
    audit(h.sess["user"], f"server-{act}")
    return {"job": jid}


@route("GET", "/api/server/job/(?P<jid>[0-9a-f]+)")
def r_job(h, q, jid):
    j = docker.job(jid, int(q.get("since", 0)))
    if not j:
        raise HttpError(404, "no such job")
    return j


@route("POST", "/api/server/autostart", role="admin")
def r_autostart(h, body):
    try:
        config.set_autostart(bool(body.get("on")))
    except ValueError as e:
        raise HttpError(400, str(e))
    audit(h.sess["user"], "autostart", str(bool(body.get("on"))))
    return {"ok": True, "note": "Takes effect the next time the containers are started."}


@route("GET", "/api/tools/dns")
def r_dns(h, q):
    name = q.get("name", "").strip()
    if name and not re.fullmatch(r"[A-Za-z0-9.-]{1,253}", name):
        raise HttpError(400, "bad name")
    return tools.dns_check(name or None)


# ---------------------------------------------------------------- storage (disks)
@route("GET", "/api/storage")
def r_storage(h, q):
    return storage.status()


@route("POST", "/api/storage/plan", role="admin")
def r_storage_plan(h, body):
    try:
        size = int(body.get("size") or 0)
    except (TypeError, ValueError):
        raise HttpError(400, "size must be a whole number of GiB")
    p = storage.plan(str(body.get("action")), [str(d) for d in body.get("devices") or []], str(body.get("fs", "ext4")),
                     size, body.get("target") if body.get("target") in ("root", "cache") else None)
    if "error" in p:
        raise HttpError(400, p["error"])
    if p.get("size") and p.get("env_change", True):
        p["env_preview"] = {"CACHE_ROOT": p["mount"], **storage.sizes_for(p["size"] * 0.98)}
    return p


@route("POST", "/api/storage/apply", role="admin")
def r_storage_apply(h, body):
    try:
        jid = storage.apply(str(body.get("action")), [str(d) for d in body.get("devices") or []], str(body.get("fs", "ext4")),
                            str(body.get("confirm", "")), h.sess["user"], int(body.get("size") or 0),
                            body.get("target") if body.get("target") in ("root", "cache") else None)
    except (ValueError, RuntimeError, TypeError) as e:
        raise HttpError(409, str(e))
    audit(h.sess["user"], "storage-" + str(body.get("action")), " ".join(body.get("devices") or []) +
          (f" {body.get('target') or ''} {body.get('size')} GiB" if body.get("size") else ""))
    return {"job": jid}


@route("POST", "/api/storage/use-folder", role="admin")
def r_storage_folder(h, body):
    try:
        b, vals = storage.use_folder(str(body.get("path", "")))
    except ValueError as e:
        raise HttpError(400, str(e))
    audit(h.sess["user"], "storage-folder", json.dumps(vals))
    return {"ok": True, "backup": b, "env": vals}


# ---------------------------------------------------------------- restart / reboot (arm-disarm switches)
@route("POST", "/api/gui/restart", role="admin")
def r_gui_restart(h, body):
    if not os.environ.get("INVOCATION_ID"):
        raise HttpError(409, "The GUI isn't running as the lancache-gui service, so nothing would start it again. Run sudo ./webgui/install.sh.")
    if docker.busy() or (prefill.current() and prefill.current().ended is None):
        raise HttpError(409, "A job is still running (disk change, server action or preload). Restarting the GUI now would kill it. Wait until it finishes.")
    audit(h.sess["user"], "gui-restart")
    # Answer first, then exit; systemd (Restart=always) starts it again.
    threading.Timer(0.8, lambda: os._exit(0)).start()
    return {"ok": True}


@route("POST", "/api/system/reboot", role="admin")
def r_reboot(h, body):
    if docker.busy():
        raise HttpError(409, "A disk change or server action is still running. Wait until it finishes before rebooting.")
    try:
        out = storage.reboot(h.sess["user"])
    except ValueError as e:
        raise HttpError(409, str(e))
    audit(h.sess["user"], "machine-reboot")
    return {"ok": True, "out": out}


# ---------------------------------------------------------------- preload (SteamPrefill)
@route("GET", "/api/prefill")
def r_prefill(h, q):
    st = prefill.status()
    local = [c for c in logs.STATS.active(30) if _is_local(c["ip"])]
    st["traffic"] = {"rate": sum(c["rate_hit"] + c["rate_miss"] for c in local), "hit": sum(c["hit"] for c in local),
                     "miss": sum(c["miss"] for c in local)}
    j = prefill.current()
    if j:
        st["job"] = prefill.job_info(j, int(q.get("since", 0)))
    return st


@route("POST", "/api/prefill/install", role="admin")
def r_prefill_install(h, body):
    try:
        return {"version": prefill.install(h.sess["user"])}
    except Exception as e:  # noqa: BLE001
        raise HttpError(502, f"Could not download SteamPrefill: {e}")


@route("POST", "/api/prefill/start", role="admin")
def r_prefill_start(h, body):
    running = {c["service"] for c in docker.status() if c["state"] == "running"} if docker.installed() else set()
    if "monolithic" not in running and not body.get("anyway"):
        raise HttpError(409, "The cache container isn't running, so a preload would download from the internet into nothing. Start the cache first.")
    if not prefill.installed():
        # First use: fetch SteamPrefill now instead of making people find the Download button.
        try:
            prefill.install(h.sess["user"])
        except Exception as e:  # noqa: BLE001
            raise HttpError(502, f"Could not download SteamPrefill (needs internet once): {e}")
    answers = {}
    if body.get("steam_user"):
        answers = {"username": str(body["steam_user"])[:64], "password": str(body.get("steam_password", ""))[:128]}
    try:
        return prefill.start(h.sess["user"], force=bool(body.get("force")), answers=answers)
    except RuntimeError as e:
        raise HttpError(409, str(e))


@route("POST", "/api/prefill/input", role="admin")
def r_prefill_input(h, body):
    j = prefill.current()
    if not j:
        raise HttpError(409, "Preload is not running")
    try:
        j.send(str(body.get("text", ""))[:200])
    except RuntimeError as e:
        raise HttpError(409, str(e))
    return {"ok": True}


@route("POST", "/api/prefill/stop", role="admin")
def r_prefill_stop(h, body):
    j = prefill.current()
    if j:
        threading.Thread(target=j.stop, daemon=True).start()
        audit(h.sess["user"], "prefill-stopped")
    return {"ok": True}


@route("POST", "/api/prefill/settings", role="admin")
def r_prefill_settings(h, body):
    patch = {}
    if "auto" in body:
        patch["prefill_auto"] = bool(body["auto"])
    if "os" in body:
        oss = [o for o in str(body["os"]).split(",") if o in ("windows", "linux", "macos")]
        if not oss:
            raise HttpError(400, "Pick at least one of windows, linux, macos")
        patch["prefill_os"] = ",".join(oss)
    if "apps" in body:
        ids = [int(a) for a in re.findall(r"\d+", str(body["apps"]))][:50]
        if not ids:
            raise HttpError(400, "Give at least one Steam app ID (CS2 is 730)")
        patch["prefill_apps"] = ids
    update_settings(patch)
    audit(h.sess["user"], "prefill-settings", json.dumps(patch))
    return {"ok": True}


@route("POST", "/api/prefill/check")
def r_prefill_check(h, body):
    return {"latest": {str(k): v for k, v in prefill.check_latest().items()}}


# ---------------------------------------------------------------- HTTP plumbing
class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "LanCacheGOOEY/" + VERSION
    sys_version = ""
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        if os.environ.get("LCGUI_ACCESS_LOG"):
            sys.stderr.write("%s %s\n" % (self.client_address[0], fmt % args))

    # --- responses
    def send_raw(self, code, data, ctype, cache=False):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "private, max-age=86400" if cache else "no-store")
        self._common_headers()
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)
        self.responded = True

    def _common_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Strict-Transport-Security", "max-age=31536000")
        self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'")
        if getattr(self, "set_cookie", None):
            self.send_header("Set-Cookie", self.set_cookie)

    def send_json(self, code, obj):
        self.send_raw(code, json.dumps(obj, default=str).encode(), "application/json")

    def start_sse(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self._common_headers()
        self.end_headers()
        self.close_connection = True
        self.responded = True

    def sse(self, obj):
        if obj is None:
            return self.sse_ping()
        self.wfile.write(b"data: " + json.dumps(obj).encode() + b"\n\n")
        self.wfile.flush()

    def sse_ping(self):
        self.wfile.write(b": ping\n\n")
        self.wfile.flush()

    # --- request handling
    def _cookie_token(self):
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == COOKIE:
                return v
        return None

    def _static(self, path):
        if path == "/":
            path = "/index.html"
        rel = os.path.normpath(path.lstrip("/"))
        full = os.path.join(STATIC_DIR, rel)
        if rel.startswith("..") or not os.path.isfile(full):
            return self.send_json(404, {"error": "not found"})
        with open(full, "rb") as f:
            data = f.read()
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype.endswith("javascript"):
            ctype += "; charset=utf-8"
        self.send_raw(200, data, ctype)

    def _dispatch(self):
        self.responded = False
        self.set_cookie = None
        url = urllib.parse.urlsplit(self.path)
        path = urllib.parse.unquote(url.path)   # e.g. /api/games/steam%3A730 -> steam:730
        if not path.startswith("/api/"):
            if self.command not in ("GET", "HEAD"):
                return self.send_json(405, {"error": "method not allowed"})
            return self._static(path)
        q = {k: v[-1] for k, v in urllib.parse.parse_qs(url.query).items()}
        method = "GET" if self.command == "HEAD" else self.command
        for m, rx, role, fn in ROUTES:
            mt = rx.match(path)
            if not mt or m != method:
                continue
            self.token = self._cookie_token()
            self.sess = auth.session(self.token)
            if role and not self.sess:
                raise HttpError(401, "Please log in.")
            if role == "admin" and self.sess["role"] != "admin":
                raise HttpError(403, "Viewers can look but not change anything. Ask an admin.")
            arg = q
            if method != "GET":
                # CSRF: JSON body plus a per-session token header.
                if role and self.headers.get("X-CSRF-Token") != self.sess["csrf"]:
                    raise HttpError(403, "Session check failed. Reload the page.")
                n = int(self.headers.get("Content-Length") or 0)
                if n > MAX_BODY:
                    raise HttpError(413, "Too large")
                raw = self.rfile.read(n) if n else b"{}"
                try:
                    arg = json.loads(raw or b"{}")
                except ValueError:
                    raise HttpError(400, "Bad JSON")
                if not isinstance(arg, dict):
                    raise HttpError(400, "Bad JSON")
            res = fn(self, arg, **mt.groupdict())
            if not self.responded:
                self.send_json(200, res if res is not None else {"ok": True})
            return
        raise HttpError(404, "No such endpoint")

    def _handle(self):
        try:
            self._dispatch()
        except HttpError as e:
            if not self.responded:
                self.send_json(e.code, {"error": e.msg})
        except (BrokenPipeError, ConnectionResetError, ssl.SSLError, TimeoutError):
            self.close_connection = True
        except Exception as e:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            if not getattr(self, "responded", False):
                self.send_json(500, {"error": f"Internal error: {e}"})

    do_GET = do_POST = do_DELETE = do_HEAD = _handle


class TLSServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 64

    def __init__(self, addr, ctx):
        self.ctx = ctx
        super().__init__(addr, Handler)

    def finish_request(self, request, client_address):
        # TLS handshake happens here, in the worker thread, so a slow client can't stall the accept loop.
        request.settimeout(30)
        try:
            conn = self.ctx.wrap_socket(request, server_side=True)
        except (ssl.SSLError, OSError):
            request.close()
            return
        conn.settimeout(None)
        try:
            Handler(conn, client_address, self)
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def handle_error(self, request, client_address):
        pass


def make_cert(force=False):
    cert, key = data_path("tls", "cert.pem"), data_path("tls", "key.pem")
    if os.path.exists(cert) and os.path.exists(key) and not force:
        return cert, key
    sans = ["DNS:localhost", "IP:127.0.0.1", f"DNS:{socket.gethostname()}"] + [f"IP:{a['ip']}" for a in host_ips()]
    code, out = run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-sha256", "-days", "3650", "-nodes",
                     "-keyout", key, "-out", cert, "-subj", f"/CN={socket.gethostname()} LAN Cache GOOEY",
                     "-addext", "subjectAltName=" + ",".join(sans)], timeout=60)
    if code != 0:
        sys.exit("Could not create a TLS certificate with openssl:\n" + out)
    os.chmod(key, 0o600)
    return cert, key


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=int(os.environ.get("LCGUI_PORT", 8443)))
    ap.add_argument("--bind", default=os.environ.get("LCGUI_BIND", "0.0.0.0"))
    ap.add_argument("--reset-password", metavar="USER", help="set a new random password for USER (creates it as admin if missing)")
    a = ap.parse_args()

    if a.reset_password:
        import secrets
        pw = secrets.token_urlsafe(9)
        existing = {u["name"]: u for u in auth.list_users()}
        auth.set_user(a.reset_password, pw, None if a.reset_password in existing else "admin")
        print(f"New password for {a.reset_password}: {pw}")
        return

    pw = auth.ensure_admin()
    cert, key = make_cert()
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(cert, key)

    logs.STATS.start()
    system.SAMPLER.start()
    threading.Thread(target=games.cover, args=(730,), daemon=True).start()   # cache CS2 art while online
    prefill.watcher(lambda: docker.installed() and any(c["service"] == "monolithic" and c["state"] == "running" for c in docker.status()))

    srv = TLSServer((a.bind, a.port), ctx)
    print(f"LAN Cache GOOEY {VERSION} on https://{primary_ip()}:{a.port}/  (lancache dir: {LANCACHE_DIR})", flush=True)
    if pw:
        print(f"First start: created user 'admin' with password: {pw}\n(also saved in {data_path('INITIAL_ADMIN_PASSWORD.txt')})", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

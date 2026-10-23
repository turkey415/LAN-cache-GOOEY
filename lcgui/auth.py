"""Users, password hashing, sessions and login rate limiting."""
import hashlib
import hmac
import secrets
import threading
import time

from .util import data_path, load_json, save_json, write_atomic

ROLES = ("admin", "viewer")
SESSION_TTL = 12 * 3600
USERS_FILE = data_path("users.json")

_lock = threading.Lock()
SESSIONS_FILE = data_path("sessions.json")
# sha256(token) -> {user, role, csrf, expires}. Only token hashes are stored, so the file can't be used to log in.
_sessions = {k: v for k, v in load_json(SESSIONS_FILE, {}).items() if v.get("expires", 0) > time.time()}
_fails = {}             # ip -> [timestamps]


def _hash(password, salt):
    return hashlib.scrypt(password.encode(), salt=salt, n=2 ** 14, r=8, p=1, dklen=32).hex()


def _tid(token):
    return hashlib.sha256(token.encode()).hexdigest()


def _save_sessions():
    save_json(SESSIONS_FILE, _sessions)


def _users():
    return load_json(USERS_FILE, {})


def ensure_admin():
    """First start: create an 'admin' account with a random password and write it to a file."""
    if _users():
        return None
    pw = secrets.token_urlsafe(9)
    set_user("admin", pw, "admin")
    path = data_path("INITIAL_ADMIN_PASSWORD.txt")
    write_atomic(path, f"username: admin\npassword: {pw}\n\nChange it after logging in, then delete this file.\n", 0o600)
    return pw


def set_user(name, password=None, role=None):
    with _lock:
        users = _users()
        u = users.get(name, {"created": time.time()})
        if password is not None:
            salt = secrets.token_bytes(16)
            u["salt"], u["hash"] = salt.hex(), _hash(password, salt)
        if role is not None:
            u["role"] = role
        users[name] = u
        save_json(USERS_FILE, users)
    if password is not None:
        drop_sessions(name)


def delete_user(name):
    with _lock:
        users = _users()
        users.pop(name, None)
        save_json(USERS_FILE, users)
    drop_sessions(name)


def list_users():
    return [{"name": n, "role": u.get("role"), "created": u.get("created"), "last_login": u.get("last_login")}
            for n, u in sorted(_users().items())]


def admin_count():
    return sum(1 for u in _users().values() if u.get("role") == "admin")


def check_password(name, password):
    u = _users().get(name)
    if not u:
        _hash(password, b"0" * 16)  # same work either way, so timing doesn't reveal usernames
        return False
    return hmac.compare_digest(_hash(password, bytes.fromhex(u["salt"])), u["hash"])


def rate_limited(ip):
    now = time.time()
    with _lock:
        recent = [t for t in _fails.get(ip, []) if now - t < 300]
        _fails[ip] = recent
        return len(recent) >= 8


def note_failure(ip):
    with _lock:
        _fails.setdefault(ip, []).append(time.time())


def login(name, password):
    if not check_password(name, password):
        return None
    with _lock:
        users = _users()
        users[name]["last_login"] = time.time()
        save_json(USERS_FILE, users)
        role = users[name]["role"]
        tok = secrets.token_urlsafe(32)
        _sessions[_tid(tok)] = {"user": name, "role": role, "csrf": secrets.token_urlsafe(24), "expires": time.time() + SESSION_TTL}
        for k in [k for k, v in _sessions.items() if v["expires"] < time.time()]:
            _sessions.pop(k)
        _save_sessions()
    return tok


def session(token):
    if not token:
        return None
    with _lock:
        tid = _tid(token)
        s = _sessions.get(tid)
        if not s:
            return None
        if s["expires"] < time.time():
            _sessions.pop(tid, None)
            return None
        # Role changes take effect immediately.
        u = _users().get(s["user"])
        if not u:
            _sessions.pop(tid, None)
            return None
        s["role"] = u.get("role", "viewer")
        return s


def logout(token):
    with _lock:
        if token:
            _sessions.pop(_tid(token), None)
            _save_sessions()


def drop_sessions(name):
    with _lock:
        for t in [t for t, s in _sessions.items() if s["user"] == name]:
            _sessions.pop(t, None)
        _save_sessions()

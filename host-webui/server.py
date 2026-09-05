#!/usr/bin/env python3
"""Gigawatt V0.1 — local accounts and the web-app shell. Playback comes later."""
import hashlib
import json
import os
import re
import secrets
import socket
import threading
import time
from http.cookies import SimpleCookie
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

ROOT = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get("WEBUI_PORT", "80"))
MUSIC_DIR = os.environ.get("MUSIC_DIR", "/data/music")
STATE_DIR = os.environ.get("STATE_DIR", "/data/gigawatt")
USERS_FILE = os.path.join(STATE_DIR, "users.json")
VERSION = "0.1"
COOKIE = "gigawatt_session"
SESSION_TTL = 30 * 24 * 3600
PBKDF2_ROUNDS = 80000
USER_RE = re.compile(r"^[A-Za-z0-9._-]{3,32}$")

LOCK = threading.Lock()
SESSIONS = {}


def _now():
    return int(time.time())


def ensure_dirs():
    for path in (STATE_DIR, MUSIC_DIR):
        try:
            os.makedirs(path, exist_ok=True)
        except OSError:
            pass


def load_users():
    try:
        with open(USERS_FILE, "r") as fh:
            data = json.load(fh)
        users = data.get("users") if isinstance(data, dict) else None
        return users if isinstance(users, list) else []
    except (OSError, ValueError):
        return []


def save_users(users):
    ensure_dirs()
    tmp = USERS_FILE + ".tmp"
    with open(tmp, "w") as fh:
        json.dump({"users": users}, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, USERS_FILE)
    try:
        os.chmod(USERS_FILE, 0o600)
    except OSError:
        pass


def hash_password(password, salt=None):
    if salt is None:
        salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("utf-8"), PBKDF2_ROUNDS
    ).hex()
    return salt, digest


def find_user(users, username):
    wanted = username.lower()
    for user in users:
        if str(user.get("username", "")).lower() == wanted:
            return user
    return None


def new_session(username):
    token = secrets.token_urlsafe(32)
    SESSIONS[token] = {"username": username, "created": _now()}
    return token


def session_user(token):
    if not token:
        return None
    rec = SESSIONS.get(token)
    if not rec:
        return None
    if _now() - rec["created"] > SESSION_TTL:
        SESSIONS.pop(token, None)
        return None
    return rec["username"]


def host_snapshot():
    hostname = socket.gethostname()
    ip = ""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.2)
        s.connect(("192.168.1.1", 80))
        ip = s.getsockname()[0]
        s.close()
    except Exception:
        try:
            ip = socket.gethostbyname(hostname)
        except Exception:
            ip = ""
    return {"hostname": hostname, "ip": ip}


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=ROOT, **kwargs)

    def log_message(self, fmt, *args):
        sys_stderr = __import__("sys").stderr
        sys_stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _json(self, code, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length < 0 or length > 1_000_000:
            return None
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None
        return data if isinstance(data, dict) else None

    def _cookie_token(self):
        raw = self.headers.get("Cookie")
        if not raw:
            return None
        jar = SimpleCookie()
        try:
            jar.load(raw)
        except Exception:
            return None
        morsel = jar.get(COOKIE)
        return morsel.value if morsel else None

    def _set_session(self, token):
        self.send_header(
            "Set-Cookie",
            "%s=%s; Path=/; HttpOnly; SameSite=Lax; Max-Age=%d"
            % (COOKIE, token, SESSION_TTL),
        )

    def _clear_session(self):
        self.send_header(
            "Set-Cookie",
            "%s=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0" % COOKIE,
        )

    def _current_user(self):
        return session_user(self._cookie_token())

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/api/auth/status":
            with LOCK:
                users = load_users()
                username = self._current_user()
            host = host_snapshot()
            self._json(
                200,
                {
                    "version": VERSION,
                    "name": "Gigawatt",
                    "setup_required": len(users) == 0,
                    "user": {"username": username} if username else None,
                    "host": host,
                    "formats": ["mp3", "flac", "opus"],
                    "playback": False,
                },
            )
            return
        if path in ("/", "/index.html"):
            self.path = "/index.html"
        return SimpleHTTPRequestHandler.do_GET(self)

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        data = self._read_json()
        if data is None:
            self._json(400, {"ok": False, "error": "invalid json"})
            return
        if path == "/api/auth/setup":
            self._setup(data)
            return
        if path == "/api/auth/login":
            self._login(data)
            return
        if path == "/api/auth/logout":
            token = self._cookie_token()
            if token:
                SESSIONS.pop(token, None)
            body = json.dumps({"ok": True}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self._clear_session()
            self.end_headers()
            self.wfile.write(body)
            return
        self._json(404, {"ok": False, "error": "not found"})

    def _setup(self, data):
        username = str(data.get("username") or "").strip()
        password = str(data.get("password") or "")
        confirm = str(data.get("confirm") or password)
        error = _validate_account(username, password, confirm)
        if error:
            self._json(400, {"ok": False, "error": error})
            return
        with LOCK:
            users = load_users()
            if users:
                self._json(409, {"ok": False, "error": "an account already exists on this host"})
                return
            salt, digest = hash_password(password)
            users.append(
                {
                    "username": username,
                    "salt": salt,
                    "hash": digest,
                    "created": _now(),
                }
            )
            save_users(users)
            token = new_session(username)
        self._auth_ok(username, token, created=True)

    def _login(self, data):
        username = str(data.get("username") or "").strip()
        password = str(data.get("password") or "")
        if not username or not password:
            self._json(400, {"ok": False, "error": "enter username and password"})
            return
        with LOCK:
            users = load_users()
            if not users:
                self._json(409, {"ok": False, "error": "create the first account first"})
                return
            user = find_user(users, username)
            if not user:
                self._json(401, {"ok": False, "error": "wrong username or password"})
                return
            _salt, digest = hash_password(password, user.get("salt") or "")
            stored = str(user.get("hash") or "")
            if len(digest) != len(stored) or not secrets.compare_digest(digest, stored):
                self._json(401, {"ok": False, "error": "wrong username or password"})
                return
            token = new_session(user["username"])
            real_name = user["username"]
        self._auth_ok(real_name, token, created=False)

    def _auth_ok(self, username, token, created):
        payload = {"ok": True, "created": created, "user": {"username": username}, "version": VERSION}
        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._set_session(token)
        self.end_headers()
        self.wfile.write(body)


def _validate_account(username, password, confirm):
    if not USER_RE.match(username):
        return "username must be 3–32 letters, numbers, dot, underscore, or dash"
    if len(password) < 8:
        return "password must be at least 8 characters"
    if len(password) > 200:
        return "password is too long"
    if password != confirm:
        return "passwords do not match"
    return None


def main():
    ensure_dirs()
    os.chdir(ROOT)
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print("Gigawatt V%s on 0.0.0.0:%s" % (VERSION, PORT), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    server.server_close()


if __name__ == "__main__":
    main()

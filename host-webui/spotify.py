#!/usr/bin/env python3
"""Spotify Connect wrapper around go-librespot (Pulse → TOSLINK)."""
import json
import os
import subprocess
import threading
import time
import urllib.error
import urllib.request

from airplay import DEFAULT_NAME, sanitize_name

API_HOST = "127.0.0.1"
API_PORT = 3678
PULSE_SOCK = "unix:/var/run/pulse/native"
CONF = """log_level: warn
device_name: %s
device_type: speaker
audio_backend: pulseaudio
audio_backend_runtime_socket: %s
bitrate: 320
external_volume: false
prefer_firewall_friendly_ports: true
zeroconf_enabled: true
zeroconf_backend: builtin
credentials:
  type: zeroconf
  zeroconf:
    persist_credentials: true
server:
  enabled: true
  address: %s
  port: %s
"""


def resolve_runtime_dir(directory):
    here = os.path.join(os.path.dirname(os.path.abspath(__file__)), "spotify")
    for path in (directory, here):
        if path and os.path.isfile(os.path.join(path, "go-librespot")):
            return path
    return directory or here


def session_from_status(status):
    """Map a go-librespot /status body to (active, title, artist, album).

    GET /status is 204 with an empty body when nobody has connected yet.
    urllib treats that as success, so callers pass None or {}.
    """
    if not isinstance(status, dict) or not status:
        return False, "", "", ""
    track = status.get("track") or {}
    if not isinstance(track, dict):
        track = {}
    name = (track.get("name") or "").strip()
    artists = track.get("artist_names") or []
    if isinstance(artists, list):
        artists = ", ".join([a for a in artists if a])
    else:
        artists = str(artists).strip()
    album = (track.get("album_name") or "").strip()
    stopped = bool(status.get("stopped"))
    username = (status.get("username") or "").strip()
    # Own the speaker only when Spotify has a session and a context.
    # Empty/204 payloads used to look "not stopped" and lock the UI.
    active = (not stopped) and bool(username or name)
    if not active:
        return False, "", "", ""
    return True, name, artists, album


class Spotify:
    def __init__(self, directory, state_dir, on_begin=None, name=None):
        self.directory = resolve_runtime_dir(directory)
        self.state_dir = state_dir
        self.on_begin = on_begin
        self.lock = threading.Lock()
        self.proc = None
        self.enabled = False
        self.active = False
        self.title = ""
        self.artist = ""
        self.album = ""
        self.error = ""
        self.name = sanitize_name(name) or DEFAULT_NAME
        try:
            self._write_conf()
        except Exception:
            pass

    def available(self):
        return os.path.isfile(os.path.join(self.directory, "go-librespot"))

    def snapshot(self):
        with self.lock:
            running = self.proc is not None and self.proc.poll() is None
            if self.proc is not None and self.proc.poll() is not None:
                if self.enabled and not self.error:
                    self.error = self._log_tail() or "Spotify Connect exited"
                self.proc = None
                self.active = False
                running = False
            if running:
                self._ingest_status_locked()
            title = self.title
            if self.active and not title:
                title = "Spotify"
            return {
                "available": self.available(),
                "enabled": bool(self.enabled and running),
                "active": bool(self.active and running),
                "name": self.name,
                "title": title if self.active else "",
                "artist": self.artist if self.active else "",
                "album": self.album if self.active else "",
                "client": "",
                "error": self.error,
            }

    def set_name(self, name):
        clean = sanitize_name(name)
        if not clean:
            self.error = "name must be 1–50 letters, numbers, space, dot, underscore, or dash"
            return False
        with self.lock:
            self.name = clean
            try:
                self._write_conf()
            except Exception as exc:
                self.error = str(exc)
                return False
            self.error = ""
            running = self.proc is not None and self.proc.poll() is None
        if running:
            try:
                self._api("POST", "/set_device_name", {"name": clean})
            except Exception:
                with self.lock:
                    self._stop_locked()
                    return self._start_locked()
        return True

    def set_enabled(self, value):
        want = bool(value)
        with self.lock:
            self.enabled = want
            if want:
                return self._start_locked()
            self._stop_locked()
            self.error = ""
            return True

    def stop_playback(self):
        with self.lock:
            running = self.proc is not None and self.proc.poll() is None
            self.active = False
            self.title = ""
            self.artist = ""
            self.album = ""
        if running:
            try:
                self._api("POST", "/player/stop", {})
            except Exception:
                pass

    def _config_dir(self):
        path = os.path.join(self.state_dir, "spotify")
        os.makedirs(path, exist_ok=True)
        return path

    def _log_path(self):
        return os.path.join(self._config_dir(), "go-librespot.log")

    def _log_tail(self, limit=8):
        try:
            with open(self._log_path(), "r") as fh:
                lines = [ln.strip() for ln in fh.readlines() if ln.strip()]
        except OSError:
            return ""
        if not lines:
            return ""
        return " | ".join(lines[-limit:])

    def _write_conf(self):
        path = os.path.join(self._config_dir(), "config.yml")
        sock = os.environ.get("PULSE_SERVER") or PULSE_SOCK
        body = CONF % (self.name.replace("\\", ""), sock, API_HOST, API_PORT)
        tmp = path + ".tmp"
        with open(tmp, "w") as fh:
            fh.write(body)
        os.replace(tmp, path)

    def _clear_stale_lock(self):
        lock = os.path.join(self._config_dir(), "lockfile")
        try:
            if os.path.exists(lock):
                os.remove(lock)
        except OSError:
            pass

    def _start_locked(self):
        if not self.available():
            self.error = "Spotify Connect binary missing"
            return False
        if self.proc is not None and self.proc.poll() is None:
            self.error = ""
            return True
        try:
            self._write_conf()
        except Exception as exc:
            self.error = str(exc)
            return False
        self._clear_stale_lock()
        env = os.environ.copy()
        env["PULSE_SERVER"] = env.get("PULSE_SERVER") or PULSE_SOCK
        env["SPOTIFY_STATE"] = self._config_dir()
        if not env.get("HOME"):
            env["HOME"] = os.path.expanduser("~") or "/home/RPM"
        cookie = os.path.join(env["HOME"], ".config", "pulse", "cookie")
        if os.path.isfile(cookie):
            env["PULSE_COOKIE"] = cookie
        cmd = os.path.join(self.directory, "run-spotify")
        if os.path.isfile(cmd) and os.access(cmd, os.X_OK):
            args = [cmd]
        else:
            args = [os.path.join(self.directory, "go-librespot"), "-config_dir", self._config_dir()]
        log_fh = None
        try:
            log_fh = open(self._log_path(), "ab")
            self.proc = subprocess.Popen(
                args,
                stdout=log_fh,
                stderr=log_fh,
                env=env,
                cwd=self.directory,
            )
        except Exception as exc:
            if log_fh is not None:
                try:
                    log_fh.close()
                except Exception:
                    pass
            self.error = str(exc)
            self.proc = None
            return False
        try:
            log_fh.close()
        except Exception:
            pass
        time.sleep(0.5)
        if self.proc.poll() is not None:
            self.error = self._log_tail() or "Spotify Connect exited at start"
            self.proc = None
            return False
        self.error = ""
        self.active = False
        threading.Thread(target=self._watch, daemon=True).start()
        return True

    def _stop_locked(self):
        proc = self.proc
        self.proc = None
        self.active = False
        self.title = ""
        self.artist = ""
        self.album = ""
        if proc is None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=4)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        self._clear_stale_lock()

    def _api(self, method, path, payload=None):
        url = "http://%s:%s%s" % (API_HOST, API_PORT, path)
        data = None
        headers = {}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=2) as resp:
                if getattr(resp, "status", 200) == 204:
                    return None
                raw = resp.read()
                if not raw:
                    return None
                return json.loads(raw.decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code == 204:
                return None
            raise

    def _apply_status_locked(self, status):
        was = self.active
        active, title, artist, album = session_from_status(status)
        self.active = active
        if active:
            if title:
                self.title = title
            if artist:
                self.artist = artist
            if album:
                self.album = album
        else:
            self.title = ""
            self.artist = ""
            self.album = ""
        if self.active and not was and self.on_begin:
            threading.Thread(target=self._fire_begin, daemon=True).start()

    def _ingest_status_locked(self):
        try:
            status = self._api("GET", "/status")
        except Exception:
            return
        self._apply_status_locked(status)

    def _fire_begin(self):
        try:
            self.on_begin()
        except Exception:
            pass

    def _watch(self):
        while True:
            with self.lock:
                proc = self.proc
            if proc is None or proc.poll() is not None:
                return
            with self.lock:
                self._ingest_status_locked()
            time.sleep(1.0)

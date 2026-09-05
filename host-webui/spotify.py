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
PULSE_SOCK_DEFAULT = "/var/run/pulse/native"
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


def pulse_socket():
    raw = os.environ.get("PULSE_SERVER") or ("unix:" + PULSE_SOCK_DEFAULT)
    if raw.startswith("unix:"):
        return raw[5:] or PULSE_SOCK_DEFAULT
    if raw.startswith("/"):
        return raw
    return PULSE_SOCK_DEFAULT


def resolve_runtime_dir(directory=None):
    candidates = []
    if directory:
        candidates.append(directory)
    here = os.path.dirname(os.path.abspath(__file__))
    candidates.append(os.path.join(here, "spotify"))
    candidates.append("/data/opt/spotify")
    seen = set()
    for path in candidates:
        path = os.path.abspath(path)
        if path in seen:
            continue
        seen.add(path)
        if os.path.isfile(os.path.join(path, "go-librespot")):
            return path
    return os.path.abspath(directory or candidates[0])


def session_from_status(status):
    """Idle unless a username or track name exists and stopped is false."""
    idle = {
        "active": False,
        "title": "",
        "artist": "",
        "album": "",
        "username": "",
    }
    if not status or not isinstance(status, dict):
        return idle
    track = status.get("track") or {}
    if not isinstance(track, dict):
        track = {}
    title = str(track.get("name") or status.get("track_name") or "").strip()
    username = str(status.get("username") or "").strip()
    if bool(status.get("stopped")) or not (username or title):
        return idle
    artists = track.get("artist_names") or []
    if isinstance(artists, list):
        artists = ", ".join([item for item in artists if item])
    else:
        artists = str(artists or "")
    album = str(track.get("album_name") or "").strip()
    return {
        "active": True,
        "title": title or "Spotify",
        "artist": artists,
        "album": album,
        "username": username,
    }


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
                self.error = self._log_tail() or self.error or "Spotify Connect stopped"
                self.proc = None
                self.active = False
                self.title = ""
                self.artist = ""
                self.album = ""
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

    def _log_tail(self, n=6):
        try:
            with open(self._log_path(), "rb") as fh:
                data = fh.read()[-4000:]
            lines = [ln.strip() for ln in data.decode("utf-8", "replace").splitlines() if ln.strip()]
            return " · ".join(lines[-n:])[:180]
        except Exception:
            return ""

    def _write_conf(self):
        path = os.path.join(self._config_dir(), "config.yml")
        body = CONF % (
            self.name.replace("\\", ""),
            pulse_socket(),
            API_HOST,
            API_PORT,
        )
        tmp = path + ".tmp"
        with open(tmp, "w") as fh:
            fh.write(body)
        os.replace(tmp, path)

    def _drop_lockfile(self):
        lock = os.path.join(self._config_dir(), "lockfile")
        try:
            if os.path.isfile(lock):
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
        self._drop_lockfile()
        env = os.environ.copy()
        sock = pulse_socket()
        env["PULSE_SERVER"] = env.get("PULSE_SERVER") or ("unix:" + sock)
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
            args = [os.path.join(self.directory, "go-librespot"), "--config_dir", self._config_dir()]
        try:
            logf = open(self._log_path(), "ab")
        except OSError as exc:
            self.error = str(exc)
            return False
        try:
            self.proc = subprocess.Popen(
                args,
                stdout=logf,
                stderr=subprocess.STDOUT,
                env=env,
                cwd=self.directory,
            )
        except Exception as exc:
            logf.close()
            self.error = str(exc)
            self.proc = None
            return False
        logf.close()
        time.sleep(0.5)
        if self.proc.poll() is not None:
            self.error = self._log_tail() or "Spotify Connect failed to start"
            self.proc = None
            return False
        self.error = ""
        self.active = False
        self.title = ""
        self.artist = ""
        self.album = ""
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
        self._drop_lockfile()

    def _api(self, method, path, payload=None):
        url = "http://%s:%s%s" % (API_HOST, API_PORT, path)
        data = None
        headers = {}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=2) as resp:
            raw = resp.read()
            if not raw:
                return {}
            try:
                parsed = json.loads(raw.decode("utf-8"))
            except ValueError:
                return {}
            return parsed if isinstance(parsed, dict) else {}

    def _ingest_status_locked(self):
        try:
            status = self._api("GET", "/status")
        except urllib.error.HTTPError as exc:
            if exc.code == 204:
                status = {}
            else:
                return
        except Exception:
            return
        sess = session_from_status(status)
        was = self.active
        self.active = sess["active"]
        self.title = sess["title"]
        self.artist = sess["artist"]
        self.album = sess["album"]
        if self.active and not was and self.on_begin:
            threading.Thread(target=self._fire_begin, daemon=True).start()

    def _fire_begin(self):
        try:
            self.on_begin()
        except Exception:
            pass

    def _watch(self):
        while True:
            with self.lock:
                proc = self.proc
            if proc is None:
                return
            if proc.poll() is not None:
                with self.lock:
                    if self.proc is proc:
                        self.proc = None
                        self.active = False
                        self.title = ""
                        self.artist = ""
                        self.album = ""
                        if self.enabled:
                            self.error = self._log_tail() or "Spotify Connect stopped"
                return
            with self.lock:
                self._ingest_status_locked()
            time.sleep(1.0)

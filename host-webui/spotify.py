#!/usr/bin/env python3
"""Spotify Connect wrapper around go-librespot (ALSA pulse → TOSLINK)."""
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
CONF = """log_level: warn
device_name: %s
device_type: speaker
audio_backend: alsa
audio_device: pulse
bitrate: 320
external_volume: false
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


class Spotify:
    def __init__(self, directory, state_dir, on_begin=None, name=None):
        self.directory = directory
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

    def _write_conf(self):
        path = os.path.join(self._config_dir(), "config.yml")
        body = CONF % (self.name.replace("\\", ""), API_HOST, API_PORT)
        tmp = path + ".tmp"
        with open(tmp, "w") as fh:
            fh.write(body)
        os.replace(tmp, path)

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
        env = os.environ.copy()
        env["PULSE_SERVER"] = env.get("PULSE_SERVER") or "unix:/var/run/pulse/native"
        env["SPOTIFY_STATE"] = self._config_dir()
        cmd = os.path.join(self.directory, "run-spotify")
        if not os.path.isfile(cmd):
            cmd = os.path.join(self.directory, "go-librespot")
            args = [cmd, "-config_dir", self._config_dir()]
        else:
            args = [cmd]
        try:
            self.proc = subprocess.Popen(
                args,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=env,
                cwd=self.directory,
            )
        except Exception as exc:
            self.error = str(exc)
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
            return json.loads(raw.decode("utf-8"))

    def _ingest_status_locked(self):
        try:
            status = self._api("GET", "/status")
        except urllib.error.HTTPError as exc:
            if exc.code == 204:
                if self.active:
                    self.active = False
                    self.title = ""
                    self.artist = ""
                    self.album = ""
                return
            return
        except Exception:
            return
        track = status.get("track") or {}
        stopped = bool(status.get("stopped"))
        paused = bool(status.get("paused"))
        was = self.active
        self.active = not stopped and bool(track.get("name") or not paused)
        if stopped:
            self.active = False
        name = track.get("name") or ""
        artists = track.get("artist_names") or []
        if isinstance(artists, list):
            artists = ", ".join([a for a in artists if a])
        album = track.get("album_name") or ""
        if name:
            self.title = name
        if artists:
            self.artist = artists
        if album:
            self.album = album
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
            if proc is None or proc.poll() is not None:
                return
            with self.lock:
                self._ingest_status_locked()
            time.sleep(1.0)

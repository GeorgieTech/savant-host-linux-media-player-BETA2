#!/usr/bin/env python3
"""AirPlay 1 receiver wrapper around shairport-sync 3.3.x (XML metadata pipe)."""
import base64
import os
import re
import stat
import subprocess
import threading
import time

META_PIPE = "/tmp/gigawatt-airplay.meta"
NAME_RE = re.compile(r"^[A-Za-z0-9._ -]{1,50}$")
DEFAULT_NAME = "Gigawatt"

CONF_TEMPLATE = """general = {
  name = "%s";
  interpolation = "basic";
  output_backend = "pa";
  ignore_volume_control = "no";
  port = 5000;
};
sessioncontrol = {
  allow_session_interruption = "yes";
  session_timeout = 120;
};
metadata = {
  enabled = "yes";
  include_cover_art = "no";
  pipe_name = "%s";
  pipe_timeout = 5000;
};
pa = {
  application_name = "Gigawatt AirPlay";
};
"""


def sanitize_name(name):
    name = (name or "").strip()
    if not NAME_RE.match(name):
        return None
    return name
ITEM_RE = re.compile(
    br"<item><type>([0-9a-fA-F]+)</type><code>([0-9a-fA-F]+)</code><length>(\d+)</length>"
    br"(?:\s*<data encoding=\"base64\">(.*?)</data>)?\s*</item>",
    re.DOTALL | re.IGNORECASE,
)


def _fourcc(hexstr):
    try:
        n = int(hexstr, 16)
    except (TypeError, ValueError):
        return "????"
    chars = []
    for shift in (24, 16, 8, 0):
        c = (n >> shift) & 0xFF
        chars.append(chr(c) if 32 <= c < 127 else "?")
    return "".join(chars)


def _decode_payload(b64):
    if not b64:
        return ""
    raw = b64.strip()
    if not raw:
        return ""
    try:
        data = base64.b64decode(raw)
    except Exception:
        return raw.decode("utf-8", "replace").strip()
    if not data:
        return ""
    if data.startswith(b"\xff\xfe") or data.startswith(b"\xfe\xff"):
        return data.decode("utf-16", "replace").replace("\x00", "").strip()
    try:
        text = data.decode("utf-8")
    except Exception:
        text = data.decode("utf-8", "replace")
    if "\x00" in text:
        try:
            text = data.decode("utf-16-be")
        except Exception:
            text = text.replace("\x00", "")
    return text.strip()


def _pulse_airplay_playing():
    try:
        out = subprocess.check_output(
            ["pactl", "list", "sink-inputs"],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=2,
        )
    except Exception:
        return False
    return "Gigawatt AirPlay" in out or "shairport" in out.lower()


class AirPlay:
    def __init__(self, directory, on_begin=None, name=None):
        self.directory = directory
        self.on_begin = on_begin
        self.lock = threading.Lock()
        self.proc = None
        self.enabled = False
        self.active = False
        self.title = ""
        self.artist = ""
        self.album = ""
        self.client = ""
        self.error = ""
        self.name = sanitize_name(name) or DEFAULT_NAME
        self._meta_fh = None
        try:
            self._write_conf()
        except Exception:
            pass

    def available(self):
        return os.path.isfile(os.path.join(self.directory, "run-shairport"))

    def snapshot(self):
        with self.lock:
            running = self.proc is not None and self.proc.poll() is None
            if self.proc is not None and self.proc.poll() is not None:
                self.proc = None
                self.active = False
                running = False
            if running and not self.active and _pulse_airplay_playing():
                self.active = True
            title = self.title
            if self.active and not title:
                title = "AirPlay"
            return {
                "available": self.available(),
                "enabled": bool(self.enabled and running),
                "active": bool(self.active and running),
                "name": self.name,
                "title": title,
                "artist": self.artist,
                "album": self.album,
                "client": self.client,
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
            if self.enabled:
                self._stop_locked()
                return self._start_locked()
            return True

    def _write_conf(self):
        path = os.path.join(self.directory, "shairport-sync.conf")
        body = CONF_TEMPLATE % (self.name.replace("\\", ""), META_PIPE)
        tmp = path + ".tmp"
        with open(tmp, "w") as fh:
            fh.write(body)
        os.replace(tmp, path)

    def set_enabled(self, value):
        want = bool(value)
        with self.lock:
            self.enabled = want
            if want:
                return self._start_locked()
            self._stop_locked()
            self.error = ""
            return True

    def _ensure_fifo(self):
        path = META_PIPE
        if os.path.exists(path) and not stat.S_ISFIFO(os.stat(path).st_mode):
            os.remove(path)
        if not os.path.exists(path):
            os.mkfifo(path, 0o666)
        try:
            os.chmod(path, 0o666)
        except Exception:
            pass
        if self._meta_fh is None:
            fd = os.open(path, os.O_RDWR)
            self._meta_fh = os.fdopen(fd, "rb", buffering=0)

    def _start_locked(self):
        if not self.available():
            self.error = "AirPlay binary missing"
            return False
        if self.proc is not None and self.proc.poll() is None:
            self.error = ""
            return True
        try:
            self._ensure_fifo()
        except Exception as exc:
            self.error = "metadata pipe: %s" % exc
            return False
        env = os.environ.copy()
        env["PULSE_SERVER"] = env.get("PULSE_SERVER") or "unix:/var/run/pulse/native"
        cmd = os.path.join(self.directory, "run-shairport")
        try:
            self.proc = subprocess.Popen(
                [cmd],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=env,
            )
        except Exception as exc:
            self.error = str(exc)
            self.proc = None
            return False
        self.error = ""
        self.active = False
        threading.Thread(target=self._meta_loop, daemon=True).start()
        threading.Thread(target=self._pulse_watch, daemon=True).start()
        return True

    def _stop_locked(self):
        proc = self.proc
        self.proc = None
        self.active = False
        self.title = ""
        self.artist = ""
        self.album = ""
        self.client = ""
        if proc is None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=3)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    def _pulse_watch(self):
        while True:
            with self.lock:
                proc = self.proc
                known_active = self.active
            if proc is None or proc.poll() is not None:
                return
            flowing = _pulse_airplay_playing()
            if flowing and not known_active:
                begin = False
                with self.lock:
                    if not self.active:
                        self.active = True
                        begin = True
                if begin and self.on_begin:
                    try:
                        self.on_begin()
                    except Exception:
                        pass
            elif not flowing and known_active:
                with self.lock:
                    if not self.title:
                        self.active = False
            time.sleep(1.0)

    def _meta_loop(self):
        buf = b""
        while True:
            with self.lock:
                proc = self.proc
                fh = self._meta_fh
            if proc is None or proc.poll() is not None or fh is None:
                return
            try:
                chunk = os.read(fh.fileno(), 4096)
            except Exception:
                time.sleep(0.2)
                continue
            if not chunk:
                time.sleep(0.05)
                continue
            buf += chunk
            buf = self._consume(buf)

    def _consume(self, buf):
        while True:
            match = ITEM_RE.search(buf)
            if not match:
                if len(buf) > 2 * 1024 * 1024:
                    buf = buf[-65536:]
                return buf
            typ, code, _length, b64 = match.group(1), match.group(2), match.group(3), match.group(4)
            key = _fourcc(typ.decode("ascii", "replace")) + "." + _fourcc(code.decode("ascii", "replace"))
            payload = _decode_payload(b64 or b"")
            self._apply(key, payload)
            buf = buf[match.end():]
        return buf

    def _apply(self, key, text):
        begin = False
        with self.lock:
            if key == "ssnc.pbeg":
                self.active = True
                begin = True
            elif key == "ssnc.pend":
                self.active = False
                self.title = ""
                self.artist = ""
                self.album = ""
            elif key == "ssnc.prsm":
                self.active = True
            elif key == "core.minm":
                if text:
                    self.title = text
                self.active = True
            elif key == "core.asar":
                if text:
                    self.artist = text
            elif key == "core.asal":
                if text:
                    self.album = text
            elif key in ("ssnc.snam", "ssnc.snua", "ssnc.clip"):
                if text:
                    self.client = text
        if begin and self.on_begin:
            try:
                self.on_begin()
            except Exception:
                pass

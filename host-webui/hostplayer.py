#!/usr/bin/env python3
"""Host TOSLINK playback: ffmpeg | paplay into Pulse."""
import os
import shlex
import signal
import subprocess
import threading
import time

MUSIC_DIR = os.environ.get("MUSIC_DIR", "/data/music")
PULSE_SINK = os.environ.get("PULSE_SINK", "@DEFAULT_SINK@")


def _cmd(args):
    try:
        return subprocess.check_output(args, stderr=subprocess.DEVNULL, text=True).strip()
    except Exception:
        return ""


class HostPlayer:
    def __init__(self, on_end=None):
        self.on_end = on_end
        self.lock = threading.Lock()
        self.proc = None
        self.name = ""
        self.paused = False
        self.t0 = 0.0
        self.offset = 0.0
        self.hold = 0.0
        self.duration = 0.0
        self.error = ""
        self.generation = 0
        self.source = "library"
        threading.Thread(target=self._watch, daemon=True).start()

    def snapshot(self):
        with self.lock:
            alive = self._alive_locked()
            return {
                "playing": bool(alive and not self.paused),
                "paused": bool(self.paused and alive),
                "name": self.name,
                "position": self._position_locked(),
                "duration": round(self.duration or 0.0, 2),
                "error": self.error,
                "source": self.source or "library",
            }

    def play(self, relname, start=0.0):
        full = os.path.realpath(os.path.join(MUSIC_DIR, relname.replace("\\", "/").lstrip("/")))
        root = os.path.realpath(MUSIC_DIR)
        if full != root and not full.startswith(root + os.sep):
            self.error = "not found"
            return False
        if not os.path.isfile(full):
            self.error = "not found"
            return False
        try:
            start = float(start or 0.0)
        except (TypeError, ValueError):
            start = 0.0
        if start < 0:
            start = 0.0
        with self.lock:
            self.source = "library"
            self.name = os.path.relpath(full, root).replace("\\", "/")
            self.duration = self._probe(full)
            if self.duration and start >= max(0.0, self.duration - 0.2):
                start = 0.0
            return self._play_locked(full, start)

    def play_url(self, url, start=0.0, title="", duration=0.0):
        url = (url or "").strip()
        if not url.startswith("http://") and not url.startswith("https://"):
            self.error = "bad url"
            return False
        try:
            start = float(start or 0.0)
        except (TypeError, ValueError):
            start = 0.0
        if start < 0:
            start = 0.0
        try:
            duration = float(duration or 0.0)
        except (TypeError, ValueError):
            duration = 0.0
        with self.lock:
            self.source = "url"
            self.name = title or url
            self.duration = duration
            return self._play_locked(url, start)

    def pause(self):
        with self.lock:
            if not self._alive_locked():
                self.error = "nothing playing"
                return False
            if self.paused:
                return True
            self.hold = self._position_locked()
            try:
                os.killpg(os.getpgid(self.proc.pid), signal.SIGSTOP)
            except Exception as exc:
                self.error = str(exc)
                return False
            self.paused = True
            self.error = ""
            return True

    def resume(self):
        with self.lock:
            if not self._alive_locked():
                self.error = "nothing playing"
                return False
            if not self.paused:
                return True
            try:
                os.killpg(os.getpgid(self.proc.pid), signal.SIGCONT)
            except Exception as exc:
                self.error = str(exc)
                return False
            self.t0 = time.monotonic() - (self.hold - self.offset)
            self.paused = False
            self.error = ""
            return True

    def stop(self):
        with self.lock:
            self._stop_locked()
            self.name = ""
            self.duration = 0.0
            self.source = "library"
            self.error = ""
            return True

    def seek(self, seconds):
        with self.lock:
            if not self.name:
                self.error = "nothing playing"
                return False
            try:
                pos = float(seconds)
            except (TypeError, ValueError):
                self.error = "bad seek"
                return False
            if pos < 0:
                pos = 0.0
            if self.duration > 0:
                pos = min(pos, max(0.0, self.duration - 0.15))
            rel = self.name
        return self.play(rel, start=pos)

    def set_volume(self, n):
        try:
            n = int(round(float(n)))
        except (TypeError, ValueError):
            return False
        n = max(0, min(100, n))
        _cmd(["pactl", "set-sink-volume", PULSE_SINK, "%s%%" % n])
        return True

    def _alive_locked(self):
        return self.proc is not None and self.proc.poll() is None

    def _position_locked(self):
        if self.paused or not self._alive_locked():
            pos = self.hold
        else:
            pos = self.offset + (time.monotonic() - self.t0)
        if self.duration > 0:
            pos = min(pos, self.duration)
        return round(max(0.0, pos), 2)

    def _probe(self, path):
        out = _cmd(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                path,
            ]
        )
        try:
            dur = float(out)
            return dur if dur > 0 else 0.0
        except (TypeError, ValueError):
            return 0.0

    def _stop_locked(self):
        proc = self.proc
        self.proc = None
        self.paused = False
        self.offset = 0.0
        self.hold = 0.0
        self.t0 = time.monotonic()
        self.generation += 1
        if proc is None:
            return
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    def _play_locked(self, path, start):
        self._stop_locked()
        ss = "-ss %.3f " % start if start > 0.04 else ""
        cmd = (
            "ffmpeg -nostdin -hide_banner -nostats -loglevel error %s-i %s "
            "-ac 2 -ar 48000 -f wav - | paplay"
            % (ss, shlex.quote(path))
        )
        try:
            self.proc = subprocess.Popen(
                cmd,
                shell=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                preexec_fn=os.setsid,
            )
        except Exception as exc:
            self.error = str(exc)
            self.proc = None
            return False
        self.offset = start
        self.hold = start
        self.t0 = time.monotonic()
        self.paused = False
        self.error = ""
        return True

    def _watch(self):
        last = -1
        while True:
            time.sleep(0.4)
            ended = False
            gen = 0
            with self.lock:
                if self.proc is not None and self.proc.poll() is not None and not self.paused:
                    if self.generation != last:
                        ended = True
                        gen = self.generation
                        self.proc = None
                        self.hold = self.duration or self._position_locked()
            if ended:
                last = gen
                if self.on_end:
                    try:
                        self.on_end()
                    except Exception:
                        pass

#!/usr/bin/env python3
"""Host TOSLINK playback: ffmpeg | paplay into Pulse, with the same 10-band EQ as the UI."""
import os
import shlex
import signal
import subprocess
import threading
import time

MUSIC_DIR = os.environ.get("MUSIC_DIR", "/data/music")
PULSE_SINK = os.environ.get("PULSE_SINK", "@DEFAULT_SINK@")
FFMPEG_LOG = os.environ.get("FFMPEG_LOG", "/tmp/gigawatt-ffmpeg.log")
EQ_Q = 1.1
EQ_BANDS = (
    ("lowshelf", 32),
    ("peaking", 64),
    ("peaking", 125),
    ("peaking", 250),
    ("peaking", 500),
    ("peaking", 1000),
    ("peaking", 2000),
    ("peaking", 4000),
    ("peaking", 8000),
    ("highshelf", 16000),
)


def _clamp_eq(values):
    out = [0.0] * len(EQ_BANDS)
    if not isinstance(values, (list, tuple)):
        return out
    for i in range(min(len(EQ_BANDS), len(values))):
        try:
            out[i] = max(-12.0, min(12.0, float(values[i])))
        except (TypeError, ValueError):
            out[i] = 0.0
    return out


def ffmpeg_eq_filter(gains):
    """Match the browser Web Audio 10-band EQ (Q 1.1, lowshelf / peaking / highshelf)."""
    gains = _clamp_eq(gains)
    parts = []
    for (kind, freq), gain in zip(EQ_BANDS, gains):
        if abs(gain) < 0.05:
            continue
        if kind == "lowshelf":
            parts.append("lowshelf=f=%s:t=q:w=%s:g=%.2f" % (freq, EQ_Q, gain))
        elif kind == "highshelf":
            parts.append("highshelf=f=%s:t=q:w=%s:g=%.2f" % (freq, EQ_Q, gain))
        else:
            parts.append("equalizer=f=%s:t=q:w=%s:g=%.2f" % (freq, EQ_Q, gain))
    return ",".join(parts)


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
        self.media = ""
        self.eq = [0.0] * len(EQ_BANDS)
        self._eq_timer = None
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

    def play(self, relname, start=0.0, root=None, origin=None):
        base = os.path.realpath(root or MUSIC_DIR)
        full = os.path.realpath(os.path.join(base, relname.replace("\\", "/").lstrip("/")))
        if full != base and not full.startswith(base + os.sep):
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
            self.source = origin or "library"
            self.name = os.path.relpath(full, base).replace("\\", "/")
            self.media = full
            self.duration = self._probe(full)
            if self.duration and start >= max(0.0, self.duration - 0.2):
                start = 0.0
            return self._play_locked(full, start)

    def play_url(self, url, start=0.0, title="", duration=0.0):
        url = (url or "").strip()
        if url.lower().startswith("https://"):
            self.error = "HTTPS streams are not supported on this host (HTTP only)"
            return False
        if not url.startswith("http://"):
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
            self.media = url
            self.duration = duration
            return self._play_locked(url, start)

    def pause(self):
        with self.lock:
            return self._pause_locked()

    def _pause_locked(self):
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
            self.media = ""
            self.duration = 0.0
            self.source = "library"
            self.error = ""
            return True

    def seek(self, seconds):
        with self.lock:
            if not self.media:
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
            return self._play_locked(self.media, pos)

    def set_eq(self, gains):
        next_eq = _clamp_eq(gains)
        with self.lock:
            same = self.eq == next_eq
            self.eq = next_eq
            if self._eq_timer is not None:
                try:
                    self._eq_timer.cancel()
                except Exception:
                    pass
                self._eq_timer = None
            if same or not self.media:
                return True
            if not self._alive_locked() and not self.paused:
                return True
            timer = threading.Timer(0.4, self._apply_eq_now)
            timer.daemon = True
            self._eq_timer = timer
            timer.start()
        return True

    def _apply_eq_now(self):
        with self.lock:
            self._eq_timer = None
            if not self.media:
                return
            if not self._alive_locked() and not self.paused:
                return
            pos = self._position_locked()
            paused = self.paused
            ok = self._play_locked(self.media, pos)
            if ok and paused:
                self._pause_locked()

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

    def _ffmpeg_tail(self):
        try:
            with open(FFMPEG_LOG, "rb") as fh:
                data = fh.read()[-2500:]
            lines = [ln.strip() for ln in data.decode("utf-8", "replace").splitlines() if ln.strip()]
            return " · ".join(lines[-4:])[:160]
        except Exception:
            return ""

    def _play_locked(self, path, start):
        self._stop_locked()
        self.media = path
        ss = "-ss %.3f " % start if start > 0.04 else ""
        af = ffmpeg_eq_filter(self.eq)
        extra = ("-af %s " % shlex.quote(af)) if af else ""
        try:
            open(FFMPEG_LOG, "w").close()
        except OSError:
            pass
        cmd = (
            "ffmpeg -nostdin -hide_banner -nostats -loglevel error %s-i %s "
            "-ac 2 -ar 48000 %s-f wav - 2>>%s | paplay"
            % (ss, shlex.quote(path), extra, shlex.quote(FFMPEG_LOG))
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
        if str(path).startswith("http://"):
            time.sleep(0.35)
            if self.proc.poll() is not None:
                self.error = self._ffmpeg_tail() or "play failed"
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

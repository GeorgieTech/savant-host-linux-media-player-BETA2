#!/usr/bin/env python3
"""Gigawatt V0.11 — library, NAS browse, browser or TOSLINK EQ, AirPlay, DLNA, Spotify."""
import cgi
import hashlib
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
from collections import OrderedDict
from http.cookies import SimpleCookie
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse, unquote

from airplay import AirPlay, DEFAULT_NAME, sanitize_name
from dlna import DlnaRenderer
from hostplayer import HostPlayer
from nas import NasShare
from spotify import Spotify

ROOT = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get("WEBUI_PORT", "80"))
MUSIC_DIR = os.environ.get("MUSIC_DIR", "/data/music")
STATE_DIR = os.environ.get("STATE_DIR", "/data/gigawatt")
USERS_FILE = os.path.join(STATE_DIR, "users.json")
LIBRARY_FILE = os.path.join(STATE_DIR, "library.json")
PLAYER_FILE = os.path.join(STATE_DIR, "player.json")
PROBE_META_FILE = os.path.join(STATE_DIR, "library-meta.json")
PROBE_CACHE_MAX = 800
VERSION = "0.11"
OUTPUTS = ("browser", "optical")
AIRPLAY_DIR = os.environ.get("AIRPLAY_DIR", "/data/opt/airplay")
SPOTIFY_DIR = os.environ.get("SPOTIFY_DIR", "/data/opt/spotify")
NAS_DIR = os.environ.get("NAS_DIR", "/data/nas")
NAS_BIN = os.environ.get("NAS_BIN", "/data/opt/nas")
NAS_TRACK_CAP = 2000
EQ_BANDS = 10
MAX_UPLOAD = 90 * 1024 * 1024
COOKIE = "gigawatt_session"
GENRES = [
    "Pop",
    "Rock",
    "Hip-Hop",
    "R&B",
    "Electronic",
    "Jazz",
    "Classical",
    "Metal",
    "Country",
    "Soundtrack",
]
SESSION_TTL = 30 * 24 * 3600
PBKDF2_ROUNDS = 80000
USER_RE = re.compile(r"^[A-Za-z0-9._-]{3,32}$")
AUDIO_EXTS = {".mp3", ".flac", ".opus", ".ogg", ".wav", ".m4a", ".aac"}
NAS_SKIP = {
    "@eadir",
    "#recycle",
    "#snapshot",
    "thumbs.db",
    "desktop.ini",
    ".ds_store",
    "albumart.jpg",
    "albumart.png",
    "folder.jpg",
    "folder.png",
    "cover.jpg",
    "cover.png",
}
NAS_UNKNOWN_ALBUM = {"unknownalbum", "unknown album", "unknown", "untitled"}
DISC_RE = re.compile(r"^(cd|disc|disk|dvd)\s*\d+$", re.I)
MIME = {
    ".mp3": "audio/mpeg",
    ".flac": "audio/flac",
    ".opus": "audio/ogg",
    ".ogg": "audio/ogg",
    ".wav": "audio/wav",
    ".m4a": "audio/mp4",
    ".aac": "audio/aac",
}

LOCK = threading.Lock()
SESSIONS = {}
PROBE_CACHE = OrderedDict()
_PROBE_LOCK = threading.Lock()
_PROBE_DIRTY = False
_PROBE_LOADED = False
_PLAYER = None
_INVENTORY = {"t": 0.0, "ident": None, "mem": None, "disk": None}
AIRPLAY = None
HOST = None
DLNA = None
SPOTIFY = None
NAS = None


def _empty_source(name=DEFAULT_NAME):
    return {
        "available": False,
        "enabled": False,
        "active": False,
        "title": "",
        "artist": "",
        "album": "",
        "client": "",
        "error": "",
        "name": name,
    }


def airplay_snapshot():
    return AIRPLAY.snapshot() if AIRPLAY is not None else _empty_source()


def dlna_snapshot():
    return DLNA.snapshot() if DLNA is not None else _empty_source()


def spotify_snapshot():
    return SPOTIFY.snapshot() if SPOTIFY is not None else _empty_source()


def nas_snapshot():
    if NAS is None:
        return {
            "available": False,
            "mounted": False,
            "enabled": False,
            "host": "",
            "share": "",
            "folder": "",
            "username": "",
            "domain": "",
            "password_set": False,
            "path": "",
            "mountpoint": NAS_DIR,
            "error": "",
            "running": False,
        }
    return NAS.snapshot()


def _on_airplay_begin():
    if HOST is not None:
        HOST.stop()
    if DLNA is not None:
        DLNA.stop_playback()
    if SPOTIFY is not None:
        SPOTIFY.stop_playback()


def _on_dlna_begin():
    if AIRPLAY is not None:
        AIRPLAY.bounce()
    if SPOTIFY is not None:
        SPOTIFY.stop_playback()


def _on_spotify_begin():
    if HOST is not None:
        HOST.stop()
    if DLNA is not None:
        DLNA.stop_playback()
    if AIRPLAY is not None:
        AIRPLAY.bounce()


def _optical_ended():
    if HOST is not None:
        snap = HOST.snapshot()
        if snap.get("source") == "url":
            if DLNA is not None:
                DLNA.on_stream_ended()
            return
        if snap.get("source") == "nas":
            return
    if load_player().get("output") != "optical":
        return
    if airplay_snapshot().get("active") or spotify_snapshot().get("active"):
        return
    tracks = list_tracks(probe=False)
    names = [t["name"] for t in tracks]
    if not names or HOST is None:
        return
    current = HOST.snapshot().get("name") or ""
    try:
        idx = names.index(current)
    except ValueError:
        idx = -1
    nxt = names[(idx + 1) % len(names)]
    HOST.play(nxt)


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


def meminfo():
    data = {}
    try:
        with open("/proc/meminfo") as fh:
            for line in fh:
                key, rest = line.split(":", 1)
                data[key] = int(rest.strip().split()[0]) * 1024
    except Exception:
        return None
    total = data.get("MemTotal")
    avail = data.get("MemAvailable") or data.get("MemFree")
    if not total:
        return None
    free = avail or 0
    return {"total": total, "used": total - free, "free": free}


def disk_usage(path="/data"):
    try:
        st = os.statvfs(path)
        total = st.f_frsize * st.f_blocks
        free = st.f_frsize * st.f_bavail
        return {"total": total, "used": total - free, "free": free, "path": path}
    except Exception:
        return None


def inventory(ttl=5.0):
    """Reuse host/RAM/disk for the 1s /api/now poll. Fresh after ttl seconds."""
    now = time.time()
    if _INVENTORY["ident"] is not None and (now - _INVENTORY["t"]) < ttl:
        return _INVENTORY["ident"], _INVENTORY["mem"], _INVENTORY["disk"]
    ident = host_snapshot()
    mem = meminfo()
    disk = disk_usage("/data")
    _INVENTORY["t"] = now
    _INVENTORY["ident"] = ident
    _INVENTORY["mem"] = mem
    _INVENTORY["disk"] = disk
    return ident, mem, disk


def _clamp_int(value, lo, hi, default):
    try:
        n = int(round(float(value)))
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


def _clamp_eq(values):
    out = [0] * EQ_BANDS
    if not isinstance(values, list):
        return out
    for i in range(min(EQ_BANDS, len(values))):
        try:
            out[i] = max(-12, min(12, float(values[i])))
        except (TypeError, ValueError):
            out[i] = 0
    return out


def _copy_player(data):
    out = dict(data)
    out["eq"] = list(data.get("eq") or [0] * EQ_BANDS)
    return out


def load_player():
    global _PLAYER
    if _PLAYER is not None:
        return _copy_player(_PLAYER)
    try:
        with open(PLAYER_FILE, "r") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            data = {}
    except (OSError, ValueError):
        data = {}
    out = data.get("output")
    if out not in OUTPUTS:
        out = "browser"
    name = sanitize_name(data.get("airplay_name")) or DEFAULT_NAME
    dlna_name = sanitize_name(data.get("dlna_name")) or DEFAULT_NAME
    spotify_name = sanitize_name(data.get("spotify_name")) or DEFAULT_NAME
    parsed = {
        "volume": _clamp_int(data.get("volume"), 0, 100, 80),
        "eq": _clamp_eq(data.get("eq")),
        "airplay": bool(data.get("airplay")),
        "airplay_name": name,
        "dlna": bool(data.get("dlna")),
        "dlna_name": dlna_name,
        "spotify": bool(data.get("spotify")),
        "spotify_name": spotify_name,
        "output": out,
    }
    _PLAYER = _copy_player(parsed)
    return parsed


def save_player(
    volume=None,
    eq=None,
    airplay=None,
    airplay_name=None,
    dlna=None,
    dlna_name=None,
    spotify=None,
    spotify_name=None,
    output=None,
):
    global _PLAYER
    current = load_player()
    if volume is not None:
        current["volume"] = _clamp_int(volume, 0, 100, current["volume"])
    if eq is not None:
        current["eq"] = _clamp_eq(eq)
    if airplay is not None:
        current["airplay"] = bool(airplay)
    if airplay_name is not None:
        clean = sanitize_name(airplay_name)
        if clean:
            current["airplay_name"] = clean
    if dlna is not None:
        current["dlna"] = bool(dlna)
    if dlna_name is not None:
        clean = sanitize_name(dlna_name)
        if clean:
            current["dlna_name"] = clean
    if spotify is not None:
        current["spotify"] = bool(spotify)
    if spotify_name is not None:
        clean = sanitize_name(spotify_name)
        if clean:
            current["spotify_name"] = clean
    if output is not None and output in OUTPUTS:
        current["output"] = output
    ensure_dirs()
    tmp = PLAYER_FILE + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(current, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, PLAYER_FILE)
    _PLAYER = _copy_player(current)
    return current


def load_tags():
    try:
        with open(LIBRARY_FILE, "r") as fh:
            data = json.load(fh)
        tags = data.get("tags") if isinstance(data, dict) else None
        return tags if isinstance(tags, dict) else {}
    except (OSError, ValueError):
        return {}


def save_tags(tags):
    ensure_dirs()
    tmp = LIBRARY_FILE + ".tmp"
    with open(tmp, "w") as fh:
        json.dump({"tags": tags}, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, LIBRARY_FILE)


def safe_music_name(name):
    name = os.path.basename((name or "").replace("\\", "/").strip())
    if not name or name in (".", "..") or ".." in name:
        return None
    ext = os.path.splitext(name)[1].lower()
    if ext not in AUDIO_EXTS:
        return None
    return name


def unique_dest(name):
    dest = os.path.join(MUSIC_DIR, name)
    if not os.path.exists(dest):
        return dest
    base, ext = os.path.splitext(name)
    n = 2
    while True:
        cand = os.path.join(MUSIC_DIR, "%s %d%s" % (base, n, ext))
        if not os.path.exists(cand):
            return cand
        n += 1


def pretty_title(name):
    base = os.path.splitext(os.path.basename(name))[0]
    base = base.replace("\uff5c", "—").replace("|", "—")
    prev = None
    while prev != base:
        prev = base
        base = re.sub(r"^\d{1,3}\s*[-.)]\s*", "", base)
    return re.sub(r"\s+", " ", base).strip() or name


def _nas_skip(name):
    if not name or name in (".", "..") or name.startswith("."):
        return True
    return name.lower() in NAS_SKIP


def _nas_rel_ok(rel):
    rel = unquote(rel or "").replace("\\", "/").strip("/")
    if not rel:
        return ""
    if rel in (".", "..") or ".." in rel.split("/"):
        return None
    return rel


def nas_abs(rel):
    rel = _nas_rel_ok(rel)
    if rel is None:
        return None
    root = os.path.realpath(NAS_DIR)
    full = os.path.realpath(os.path.join(NAS_DIR, rel)) if rel else root
    if full != root and not full.startswith(root + os.sep):
        return None
    return full


def parse_nas_meta(rel):
    parts = [p for p in (rel or "").replace("\\", "/").split("/") if p]
    if not parts:
        return "", "", ""
    title = pretty_title(parts[-1])
    folders = parts[:-1]
    while folders and DISC_RE.match(folders[-1]):
        folders.pop()
    artist = folders[0] if folders else ""
    album = folders[1] if len(folders) > 1 else ""
    if album and album.lower() in NAS_UNKNOWN_ALBUM:
        album = "Unknown album"
    return artist, album, title


def _album_label(name):
    if name and name.lower() in NAS_UNKNOWN_ALBUM:
        return "Unknown album"
    return name


def empty_nas_catalog(rel=""):
    rel = rel or ""
    crumbs = [{"label": "NAS", "path": ""}]
    acc = []
    for part in [p for p in rel.split("/") if p]:
        acc.append(part)
        crumbs.append({"label": _album_label(part), "path": "/".join(acc)})
    parent = "/".join(acc[:-1]) if acc else ""
    return {
        "path": rel,
        "parent": parent,
        "crumbs": crumbs,
        "artists": [],
        "albums": [],
        "tracks": [],
        "error": "",
        "capped": False,
    }


def _nas_track(rel):
    artist, album, title = parse_nas_meta(rel)
    ext = os.path.splitext(rel)[1].lower()
    return {
        "name": rel,
        "title": title,
        "artist": artist,
        "album": album,
        "ext": ext.lstrip("."),
        "origin": "nas",
    }


def _scandir_nas(rel):
    full = nas_abs(rel)
    dirs = []
    files = []
    if not full or not os.path.isdir(full):
        return dirs, files, "folder not found"
    try:
        with os.scandir(full) as it:
            for entry in it:
                name = entry.name
                if _nas_skip(name):
                    continue
                child = (rel + "/" + name) if rel else name
                try:
                    is_dir = entry.is_dir(follow_symlinks=False)
                    is_file = entry.is_file(follow_symlinks=False)
                except OSError:
                    continue
                if is_dir:
                    dirs.append((name, child))
                elif is_file:
                    files.append((name, child))
    except OSError as exc:
        return [], [], str(exc)
    dirs.sort(key=lambda item: item[0].lower())
    files.sort(key=lambda item: item[0].lower())
    return dirs, files, ""


def list_nas_tracks(rel="", cap=None):
    cap = NAS_TRACK_CAP if cap is None else cap
    rel = _nas_rel_ok(rel)
    if rel is None:
        return [], False
    full = nas_abs(rel)
    tracks = []
    if not full or not os.path.isdir(full):
        return tracks, False
    root = os.path.realpath(NAS_DIR)
    capped = False
    try:
        for dirpath, dirnames, filenames in os.walk(full):
            dirnames[:] = [name for name in dirnames if not _nas_skip(name)]
            dirnames.sort(key=lambda name: name.lower())
            filenames.sort(key=lambda name: name.lower())
            for filename in filenames:
                if _nas_skip(filename):
                    continue
                ext = os.path.splitext(filename)[1].lower()
                if ext not in AUDIO_EXTS:
                    continue
                child = os.path.relpath(os.path.join(dirpath, filename), root).replace("\\", "/")
                tracks.append(_nas_track(child))
                if len(tracks) >= cap:
                    capped = True
                    break
            if capped:
                break
    except OSError:
        pass
    return tracks, capped


def browse_nas(rel="", deep=False):
    rel = _nas_rel_ok(rel)
    if rel is None:
        catalog = empty_nas_catalog("")
        catalog["error"] = "path is not valid"
        return catalog
    catalog = empty_nas_catalog(rel)
    if not os.path.isdir(NAS_DIR):
        catalog["error"] = "NAS is not mounted"
        return catalog
    dirs, files, err = _scandir_nas(rel)
    if err:
        catalog["error"] = err
        return catalog
    parts = [p for p in rel.split("/") if p]
    depth = len(parts)
    disc_dirs = [(name, child) for name, child in dirs if DISC_RE.match(name)]
    other_dirs = [(name, child) for name, child in dirs if not DISC_RE.match(name)]
    if depth == 0:
        catalog["artists"] = [{"name": name, "path": child} for name, child in dirs]
    elif depth >= 2 and disc_dirs and not other_dirs:
        extra = []
        for _name, child in disc_dirs:
            more, _capped = list_nas_tracks(child, cap=NAS_TRACK_CAP - len(extra))
            extra.extend(more)
            if len(extra) >= NAS_TRACK_CAP:
                catalog["capped"] = True
                break
        catalog["tracks"].extend(extra)
    else:
        artist_name = parts[0] if parts else ""
        catalog["albums"] = [
            {"name": _album_label(name), "path": child, "artist": artist_name}
            for name, child in dirs
        ]
    for name, child in files:
        ext = os.path.splitext(name)[1].lower()
        if ext not in AUDIO_EXTS:
            continue
        catalog["tracks"].append(_nas_track(child))
        if len(catalog["tracks"]) >= NAS_TRACK_CAP:
            catalog["capped"] = True
            break
    if deep and not catalog["capped"] and depth == 1:
        nested, capped = list_nas_tracks(rel)
        seen = {track["name"] for track in catalog["tracks"]}
        for track in nested:
            if track["name"] in seen:
                continue
            catalog["tracks"].append(track)
            seen.add(track["name"])
            if len(catalog["tracks"]) >= NAS_TRACK_CAP:
                capped = True
                break
        catalog["capped"] = catalog["capped"] or capped
        catalog["tracks"].sort(key=lambda track: ((track.get("album") or "").lower(), (track.get("name") or "").lower()))
    return catalog


def fmt_hz(value):
    try:
        hz = int(value)
    except (TypeError, ValueError):
        return ""
    if hz <= 0:
        return ""
    if hz % 1000 == 0:
        return "%d kHz" % (hz // 1000)
    return ("%0.1f kHz" % (hz / 1000.0)).replace(".0 kHz", " kHz")


def fmt_bitrate(value):
    try:
        bps = int(value)
    except (TypeError, ValueError):
        return ""
    if bps <= 0:
        return ""
    return "%d kb/s" % int(round(bps / 1000.0))


def _probe_key(rel, size, mtime):
    return "%s|%s|%s" % (rel or "", int(size or 0), int(mtime or 0))


def load_probe_cache():
    global _PROBE_LOADED
    with _PROBE_LOCK:
        if _PROBE_LOADED:
            return
        _PROBE_LOADED = True
        try:
            with open(PROBE_META_FILE, "r") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return
        items = data.get("probes") if isinstance(data, dict) else None
        if not isinstance(items, list):
            return

        def _safe_int(value):
            try:
                return int(value or 0)
            except (TypeError, ValueError):
                return 0

        for item in items[-PROBE_CACHE_MAX:]:
            if not isinstance(item, dict):
                continue
            key = item.get("k")
            info = item.get("v")
            if not key or not isinstance(info, dict):
                continue
            PROBE_CACHE[key] = {
                "sample_rate": _safe_int(info.get("sample_rate")),
                "bit_rate": _safe_int(info.get("bit_rate")),
                "hz": info.get("hz") or "",
                "bitrate": info.get("bitrate") or "",
            }


def save_probe_cache():
    global _PROBE_DIRTY
    with _PROBE_LOCK:
        if not _PROBE_DIRTY:
            return
        items = [{"k": key, "v": info} for key, info in PROBE_CACHE.items()]
        _PROBE_DIRTY = False
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        tmp = PROBE_META_FILE + ".tmp"
        with open(tmp, "w") as fh:
            json.dump({"probes": items}, fh, separators=(",", ":"))
        os.replace(tmp, PROBE_META_FILE)
    except OSError:
        with _PROBE_LOCK:
            _PROBE_DIRTY = True


def probe_audio(full, size, mtime, rel=""):
    load_probe_cache()
    key = _probe_key(rel or os.path.basename(full), size, mtime)
    with _PROBE_LOCK:
        cached = PROBE_CACHE.get(key)
        if cached is not None:
            PROBE_CACHE.move_to_end(key)
            return dict(cached)
    info = {"sample_rate": 0, "bit_rate": 0, "hz": "", "bitrate": ""}
    try:
        raw = subprocess.check_output(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "a:0",
                "-show_entries",
                "stream=sample_rate,bit_rate",
                "-show_entries",
                "format=bit_rate",
                "-of",
                "json",
                full,
            ],
            stderr=subprocess.DEVNULL,
            timeout=4,
        )
        data = json.loads(raw.decode("utf-8") or "{}")
        streams = data.get("streams") or []
        stream = streams[0] if streams else {}
        fmt = data.get("format") or {}
        def _int(value):
            try:
                return int(value)
            except (TypeError, ValueError):
                return 0

        info["sample_rate"] = _int(stream.get("sample_rate"))
        info["bit_rate"] = _int(stream.get("bit_rate")) or _int(fmt.get("bit_rate"))
        info["hz"] = fmt_hz(info["sample_rate"])
        info["bitrate"] = fmt_bitrate(info["bit_rate"])
    except Exception:
        pass
    global _PROBE_DIRTY
    with _PROBE_LOCK:
        PROBE_CACHE[key] = info
        PROBE_CACHE.move_to_end(key)
        while len(PROBE_CACHE) > PROBE_CACHE_MAX:
            PROBE_CACHE.popitem(last=False)
        _PROBE_DIRTY = True
    return info


def list_tracks(root=None, probe=True, origin="local"):
    root = os.path.realpath(root or MUSIC_DIR)
    tracks = []
    if not os.path.isdir(root):
        return tracks
    tags = load_tags() if origin == "local" else {}
    count = 0
    for dirpath, _dirnames, filenames in os.walk(root):
        for filename in filenames:
            ext = os.path.splitext(filename)[1].lower()
            if ext not in AUDIO_EXTS:
                continue
            full = os.path.join(dirpath, filename)
            if not os.path.isfile(full):
                continue
            rel = os.path.relpath(full, root).replace("\\", "/")
            if rel.startswith("."):
                continue
            try:
                st = os.stat(full)
                size = st.st_size
                mtime = int(st.st_mtime)
            except OSError:
                size = 0
                mtime = 0
            if probe and size:
                info = probe_audio(full, size, mtime, rel)
            else:
                info = {"sample_rate": 0, "bit_rate": 0, "hz": "", "bitrate": ""}
            artist, album, title = parse_nas_meta(rel) if origin == "nas" else ("", "", pretty_title(rel))
            tracks.append(
                {
                    "name": rel,
                    "title": title,
                    "artist": artist,
                    "album": album,
                    "ext": ext.lstrip("."),
                    "bytes": size,
                    "sample_rate": info["sample_rate"],
                    "bit_rate": info["bit_rate"],
                    "hz": info["hz"],
                    "bitrate": info["bitrate"],
                    "genre": tags.get(rel) or "",
                    "origin": origin,
                }
            )
            count += 1
            if origin == "nas" and count >= NAS_TRACK_CAP:
                break
        if origin == "nas" and count >= NAS_TRACK_CAP:
            break
    tracks.sort(key=lambda t: t["title"].lower())
    if probe:
        save_probe_cache()
    return tracks


def safe_media_path(name, origin="local"):
    name = unquote(name or "").replace("\\", "/").lstrip("/")
    if not name or name in (".", "..") or ".." in name.split("/"):
        return None
    ext = os.path.splitext(name)[1].lower()
    if ext not in AUDIO_EXTS:
        return None
    base = NAS_DIR if origin == "nas" else MUSIC_DIR
    root = os.path.realpath(base)
    full = os.path.realpath(os.path.join(base, name))
    if full != root and not full.startswith(root + os.sep):
        return None
    if not os.path.isfile(full):
        return None
    return full


def send_media(handler, full, head=False):
    size = os.path.getsize(full)
    ext = os.path.splitext(full)[1].lower()
    mime = MIME.get(ext, "application/octet-stream")
    start = 0
    end = size - 1
    code = 200
    rng = handler.headers.get("Range") or ""
    if rng.startswith("bytes=") and size > 0:
        spec = rng.split("=", 1)[1].split("-")
        try:
            if spec[0]:
                start = int(spec[0])
            if len(spec) > 1 and spec[1]:
                end = int(spec[1])
        except ValueError:
            handler.send_error(400, "bad range")
            return
        end = min(end, size - 1)
        if start < 0 or start > end:
            handler.send_response(416)
            handler.send_header("Content-Range", "bytes */%s" % size)
            handler.end_headers()
            return
        code = 206
    length = end - start + 1
    handler.send_response(code)
    handler.send_header("Content-Type", mime)
    handler.send_header("Content-Length", str(length))
    handler.send_header("Accept-Ranges", "bytes")
    handler.send_header("Cache-Control", "private, max-age=120")
    if code == 206:
        handler.send_header("Content-Range", "bytes %s-%s/%s" % (start, end, size))
    handler.end_headers()
    if head:
        return
    with open(full, "rb") as fh:
        fh.seek(start)
        left = length
        while left > 0:
            chunk = fh.read(min(65536, left))
            if not chunk:
                break
            handler.wfile.write(chunk)
            left -= len(chunk)


def set_genre(name, genre):
    full = safe_media_path(name)
    if not full:
        return False, "not found"
    genre = (genre or "").strip()
    if genre and genre not in GENRES:
        return False, "unknown genre"
    rel = os.path.relpath(full, os.path.realpath(MUSIC_DIR)).replace("\\", "/")
    with LOCK:
        tags = load_tags()
        if genre:
            tags[rel] = genre
        else:
            tags.pop(rel, None)
        try:
            save_tags(tags)
        except Exception as exc:
            return False, str(exc)
    return True, ""


def delete_track(name):
    full = safe_media_path(name)
    if not full:
        return False, "not found"
    rel = os.path.relpath(full, os.path.realpath(MUSIC_DIR)).replace("\\", "/")
    try:
        os.remove(full)
    except OSError as exc:
        return False, str(exc)
    with LOCK:
        tags = load_tags()
        if rel in tags:
            tags.pop(rel, None)
            try:
                save_tags(tags)
            except Exception:
                pass
    return True, ""


def commit_library(tags_map, delete_names):
    errors = []
    removed = set()
    if not isinstance(delete_names, list):
        delete_names = []
    if not isinstance(tags_map, dict):
        tags_map = {}
    for name in delete_names:
        ok, err = delete_track(name)
        if ok:
            removed.add(name)
        else:
            errors.append("%s: %s" % (name, err or "could not delete"))
    with LOCK:
        tags = load_tags()
        root = os.path.realpath(MUSIC_DIR)
        for name, genre in tags_map.items():
            if name in removed:
                continue
            full = safe_media_path(name)
            if not full:
                errors.append("%s: not found" % name)
                continue
            genre = (genre or "").strip()
            if genre and genre not in GENRES:
                errors.append("%s: unknown genre" % name)
                continue
            rel = os.path.relpath(full, root).replace("\\", "/")
            if genre:
                tags[rel] = genre
            else:
                tags.pop(rel, None)
        try:
            save_tags(tags)
        except Exception as exc:
            errors.append(str(exc))
    return errors


def save_uploads(handler):
    length = int(handler.headers.get("Content-Length") or 0)
    if length > MAX_UPLOAD * 8:
        return None, "upload too large"
    form = cgi.FieldStorage(
        fp=handler.rfile,
        headers=handler.headers,
        environ={
            "REQUEST_METHOD": "POST",
            "CONTENT_TYPE": handler.headers.get("Content-Type", ""),
            "CONTENT_LENGTH": str(length),
        },
        keep_blank_values=True,
    )
    if "file" not in form:
        return None, "no file field"
    raw = form["file"]
    items = raw if isinstance(raw, list) else [raw]
    saved = []
    errors = []
    os.makedirs(MUSIC_DIR, exist_ok=True)
    for item in items:
        filename = getattr(item, "filename", None)
        if not filename:
            continue
        name = safe_music_name(filename)
        if not name:
            errors.append("rejected %s" % os.path.basename(filename))
            continue
        dest = unique_dest(name)
        tmp = dest + ".part"
        try:
            with open(tmp, "wb") as out:
                shutil.copyfileobj(item.file, out)
                size = out.tell()
            if size == 0:
                os.remove(tmp)
                errors.append("%s empty" % name)
                continue
            if size > MAX_UPLOAD:
                os.remove(tmp)
                errors.append("%s exceeds 90 MB" % name)
                continue
            os.replace(tmp, dest)
            saved.append({"name": os.path.basename(dest), "bytes": size})
        except Exception as exc:
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except Exception:
                pass
            errors.append("%s: %s" % (name, exc))
    return {"saved": saved, "errors": errors}, None


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=ROOT, **kwargs)

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

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

    def _need_user(self):
        user = self._current_user()
        if user:
            return user
        self._json(401, {"ok": False, "error": "sign in first"})
        return None

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/api/auth/status":
            with LOCK:
                users = load_users()
                username = self._current_user()
            ident = host_snapshot()
            tracks = list_tracks(probe=False) if username else []
            player = load_player()
            self._json(
                200,
                {
                    "version": VERSION,
                    "name": "Gigawatt",
                    "setup_required": len(users) == 0,
                    "user": {"username": username} if username else None,
                    "hostname": ident.get("hostname"),
                    "ip": ident.get("ip"),
                    "mem": meminfo(),
                    "disk": disk_usage("/data"),
                    "formats": ["mp3", "flac", "opus"],
                    "playback": True,
                    "tracks": tracks,
                    "genres": list(GENRES),
                    "volume": player["volume"],
                    "eq": player["eq"],
                    "output": player["output"],
                    "outputs": list(OUTPUTS),
                    "host": HOST.snapshot() if HOST is not None else {},
                    "airplay": airplay_snapshot(),
                    "dlna": dlna_snapshot(),
                    "spotify": spotify_snapshot(),
                    "nas": nas_snapshot(),
                },
            )
            return
        if path in ("/api/airplay", "/api/now", "/api/dlna", "/api/spotify", "/api/nas"):
            if not self._need_user():
                return
            player = load_player()
            host = HOST.snapshot() if HOST is not None else {}
            ident, mem, disk = inventory()
            self._json(
                200,
                {
                    "ok": True,
                    "output": player["output"],
                    "volume": player["volume"],
                    "host": host,
                    "airplay": airplay_snapshot(),
                    "dlna": dlna_snapshot(),
                    "spotify": spotify_snapshot(),
                    "nas": nas_snapshot(),
                    "hostname": ident.get("hostname"),
                    "ip": ident.get("ip"),
                    "mem": mem,
                    "disk": disk,
                },
            )
            return
        if path == "/api/library":
            if not self._need_user():
                return
            self._json(200, {"ok": True, "tracks": list_tracks(), "genres": list(GENRES)})
            return
        if path in ("/api/nas/library", "/api/nas/browse"):
            if not self._need_user():
                return
            snap = nas_snapshot()
            qs = parse_qs(parsed.query)
            rel = (qs.get("path") or [""])[0]
            deep = (qs.get("deep") or [""])[0].lower() in ("1", "true", "yes")
            catalog = browse_nas(rel, deep=deep) if snap.get("mounted") else empty_nas_catalog(rel)
            payload = {"ok": True, "nas": snap}
            payload.update(catalog)
            self._json(200, payload)
            return
        if path == "/api/media":
            if not self._current_user():
                self._json(401, {"ok": False, "error": "sign in first"})
                return
            qs = parse_qs(parsed.query)
            name = (qs.get("name") or [""])[0]
            origin = (qs.get("src") or qs.get("origin") or ["local"])[0]
            full = safe_media_path(name, origin="nas" if origin == "nas" else "local")
            if not full:
                self._json(404, {"ok": False, "error": "not found"})
                return
            send_media(self, full, head=False)
            return
        if path in ("/", "/index.html"):
            self.path = "/index.html"
        return SimpleHTTPRequestHandler.do_GET(self)

    def do_HEAD(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/media":
            if not self._current_user():
                self.send_error(401)
                return
            qs = parse_qs(parsed.query)
            name = (qs.get("name") or [""])[0]
            origin = (qs.get("src") or qs.get("origin") or ["local"])[0]
            full = safe_media_path(name, origin="nas" if origin == "nas" else "local")
            if not full:
                self.send_error(404)
                return
            send_media(self, full, head=True)
            return
        return SimpleHTTPRequestHandler.do_HEAD(self)

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/api/upload":
            if not self._need_user():
                return
            payload, err = save_uploads(self)
            if err:
                self._json(400, {"ok": False, "error": err})
                return
            code = 200 if payload.get("saved") else 400
            payload["ok"] = bool(payload.get("saved"))
            payload["tracks"] = list_tracks()
            self._json(code, payload)
            return
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
        if path == "/api/volume":
            if not self._need_user():
                return
            player = save_player(volume=data.get("volume"))
            if HOST is not None:
                HOST.set_volume(player["volume"])
            self._json(200, {"ok": True, "volume": player["volume"]})
            return
        if path == "/api/output":
            if not self._need_user():
                return
            value = data.get("output")
            if value not in OUTPUTS:
                self._json(400, {"ok": False, "error": "output must be browser or optical"})
                return
            if HOST is not None:
                HOST.stop()
            player = save_player(output=value)
            self._json(200, {"ok": True, "output": player["output"]})
            return
        if path == "/api/play":
            if not self._need_user():
                return
            if airplay_snapshot().get("active"):
                self._json(409, {"ok": False, "error": "AirPlay is playing"})
                return
            if spotify_snapshot().get("active"):
                SPOTIFY.stop_playback()
            if DLNA is not None:
                DLNA.stop_playback()
            if HOST is None:
                self._json(409, {"ok": False, "error": "host player missing"})
                return
            origin = data.get("source") or data.get("origin") or "local"
            if origin == "nas":
                ok = HOST.play(data.get("name") or "", start=data.get("start") or 0, root=NAS_DIR, origin="nas")
            else:
                ok = HOST.play(data.get("name") or "", start=data.get("start") or 0, origin="library")
            player = load_player()
            HOST.set_volume(player["volume"])
            self._json(200 if ok else 400, {"ok": ok, "host": HOST.snapshot(), "error": HOST.snapshot().get("error")})
            return
        if path == "/api/pause":
            if not self._need_user():
                return
            snap = HOST.snapshot() if HOST is not None else {}
            if snap.get("paused"):
                ok = HOST.resume()
            else:
                ok = HOST.pause() if HOST is not None else False
            self._json(200 if ok else 400, {"ok": ok, "host": HOST.snapshot() if HOST else {}})
            return
        if path == "/api/stop":
            if not self._need_user():
                return
            if HOST is not None:
                HOST.stop()
            self._json(200, {"ok": True, "host": HOST.snapshot() if HOST else {}})
            return
        if path == "/api/seek":
            if not self._need_user():
                return
            if HOST is None:
                self._json(409, {"ok": False, "error": "host player missing"})
                return
            ok = HOST.seek(data.get("seconds"))
            self._json(200 if ok else 400, {"ok": ok, "host": HOST.snapshot()})
            return
        if path == "/api/eq":
            if not self._need_user():
                return
            player = save_player(eq=data.get("eq"))
            if HOST is not None:
                HOST.set_eq(player["eq"])
            self._json(200, {"ok": True, "eq": player["eq"], "host": HOST.snapshot() if HOST else {}})
            return
        if path == "/api/airplay":
            if not self._need_user():
                return
            if AIRPLAY is None:
                self._json(409, {"ok": False, "error": "AirPlay is not available on this host"})
                return
            if "name" in data and data.get("name") is not None:
                clean = sanitize_name(data.get("name"))
                if not clean:
                    self._json(400, {"ok": False, "error": "name must be 1–50 letters, numbers, space, dot, underscore, or dash"})
                    return
                ok = AIRPLAY.set_name(clean)
                if not ok:
                    snap = airplay_snapshot()
                    snap["ok"] = False
                    self._json(400, snap)
                    return
                save_player(airplay_name=clean)
            if "enabled" in data or "airplay" in data:
                want = data.get("enabled")
                if want is None:
                    want = data.get("airplay")
                want = bool(want)
                save_player(airplay=want)
                if want and HOST is not None:
                    HOST.stop()
                ok = AIRPLAY.set_enabled(want)
                snap = airplay_snapshot()
                snap["ok"] = ok
                self._json(200 if ok else 409, snap)
                return
            snap = airplay_snapshot()
            snap["ok"] = True
            self._json(200, snap)
            return
        if path == "/api/dlna":
            if not self._need_user():
                return
            if DLNA is None:
                self._json(409, {"ok": False, "error": "DLNA is not available on this host"})
                return
            if "name" in data and data.get("name") is not None:
                clean = sanitize_name(data.get("name"))
                if not clean:
                    self._json(400, {"ok": False, "error": "name must be 1–50 letters, numbers, space, dot, underscore, or dash"})
                    return
                ok = DLNA.set_name(clean)
                if not ok:
                    snap = dlna_snapshot()
                    snap["ok"] = False
                    self._json(400, snap)
                    return
                save_player(dlna_name=clean)
            if "enabled" in data or "dlna" in data:
                want = data.get("enabled")
                if want is None:
                    want = data.get("dlna")
                want = bool(want)
                save_player(dlna=want)
                ok = DLNA.set_enabled(want)
                snap = dlna_snapshot()
                snap["ok"] = ok
                self._json(200 if ok else 409, snap)
                return
            snap = dlna_snapshot()
            snap["ok"] = True
            self._json(200, snap)
            return
        if path == "/api/spotify":
            if not self._need_user():
                return
            if SPOTIFY is None:
                self._json(409, {"ok": False, "error": "Spotify Connect is not available on this host"})
                return
            if "name" in data and data.get("name") is not None:
                clean = sanitize_name(data.get("name"))
                if not clean:
                    self._json(400, {"ok": False, "error": "name must be 1–50 letters, numbers, space, dot, underscore, or dash"})
                    return
                ok = SPOTIFY.set_name(clean)
                if not ok:
                    snap = spotify_snapshot()
                    snap["ok"] = False
                    self._json(400, snap)
                    return
                save_player(spotify_name=clean)
            if "enabled" in data or "spotify" in data:
                want = data.get("enabled")
                if want is None:
                    want = data.get("spotify")
                want = bool(want)
                save_player(spotify=want)
                if want and HOST is not None:
                    HOST.stop()
                ok = SPOTIFY.set_enabled(want)
                snap = spotify_snapshot()
                snap["ok"] = ok
                self._json(200 if ok else 409, snap)
                return
            snap = spotify_snapshot()
            snap["ok"] = True
            self._json(200, snap)
            return
        if path == "/api/nas":
            if not self._need_user():
                return
            if NAS is None:
                self._json(409, {"ok": False, "error": "NAS tools are not available on this host"})
                return
            if not NAS.apply(data):
                snap = nas_snapshot()
                snap["ok"] = False
                self._json(400, snap)
                return
            want = data.get("enabled")
            if want is None and "connect" in data:
                want = data.get("connect")
            if want is True:
                ok = NAS.connect()
                snap = nas_snapshot()
                snap["ok"] = ok
                self._json(200 if ok else 409, snap)
                return
            if want is False:
                NAS.disconnect()
                snap = nas_snapshot()
                snap["ok"] = True
                snap["tracks"] = []
                self._json(200, snap)
                return
            snap = nas_snapshot()
            snap["ok"] = True
            self._json(200, snap)
            return
        if path == "/api/library/save":
            if not self._need_user():
                return
            errors = commit_library(data.get("tags"), data.get("delete"))
            self._json(200, {"ok": not errors, "errors": errors, "tracks": list_tracks()})
            return
        if path == "/api/tag":
            if not self._need_user():
                return
            ok, err = set_genre(data.get("name"), data.get("genre"))
            if not ok:
                self._json(400, {"ok": False, "error": err})
                return
            self._json(200, {"ok": True, "tracks": list_tracks()})
            return
        if path == "/api/delete":
            if not self._need_user():
                return
            ok, err = delete_track(data.get("name"))
            if not ok:
                self._json(400, {"ok": False, "error": err})
                return
            self._json(200, {"ok": True, "tracks": list_tracks()})
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
    global AIRPLAY, HOST, DLNA, SPOTIFY, NAS
    ensure_dirs()
    os.chdir(ROOT)
    HOST = HostPlayer(on_end=_optical_ended)
    player = load_player()
    HOST.set_volume(player["volume"])
    HOST.set_eq(player["eq"])
    AIRPLAY = AirPlay(AIRPLAY_DIR, on_begin=_on_airplay_begin, name=player.get("airplay_name"))
    if player.get("airplay"):
        AIRPLAY.set_enabled(True)
    DLNA = DlnaRenderer(
        HOST,
        on_begin=_on_dlna_begin,
        name=player.get("dlna_name"),
        volume_getter=lambda: load_player()["volume"],
        uuid_path=os.path.join(STATE_DIR, "dlna-uuid"),
    )
    if player.get("dlna"):
        DLNA.set_enabled(True)
    SPOTIFY = Spotify(
        SPOTIFY_DIR,
        STATE_DIR,
        on_begin=_on_spotify_begin,
        name=player.get("spotify_name"),
    )
    if player.get("spotify"):
        SPOTIFY.set_enabled(True)
    try:
        os.makedirs(NAS_DIR, exist_ok=True)
    except OSError:
        pass
    NAS = NasShare(NAS_BIN, NAS_DIR, STATE_DIR)
    if NAS.cfg.get("enabled"):
        NAS.connect()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print("Gigawatt V%s on 0.0.0.0:%s" % (VERSION, PORT), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    server.server_close()


if __name__ == "__main__":
    main()

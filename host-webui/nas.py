#!/usr/bin/env python3
"""SMB/NAS mount via rclone FUSE. Kernel CIFS is not in this Savant image."""
import os
import re
import signal
import subprocess
import threading
import time

HOST_RE = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
SHARE_RE = re.compile(r"^[A-Za-z0-9._ $()-]{1,80}$")
FOLDER_RE = re.compile(r"^[A-Za-z0-9._ /$()-]{0,120}$")
USER_RE = re.compile(r"^[A-Za-z0-9._\\@-]{0,64}$")
DOMAIN_RE = re.compile(r"^[A-Za-z0-9._-]{0,64}$")


def _run(args, timeout=20, env=None):
    try:
        return subprocess.check_output(
            args, stderr=subprocess.STDOUT, timeout=timeout, env=env, text=True
        )
    except subprocess.CalledProcessError as exc:
        return (exc.output or "") if isinstance(exc.output, str) else ""
    except Exception as exc:
        return str(exc)


class NasShare:
    def __init__(self, directory, mountpoint, state_dir):
        self.directory = directory
        self.mountpoint = mountpoint
        self.state_dir = state_dir
        self.lock = threading.Lock()
        self.proc = None
        self.error = ""
        self.cfg = {
            "host": "",
            "share": "",
            "folder": "",
            "username": "",
            "password": "",
            "domain": "",
            "enabled": False,
        }
        self._load()

    def available(self):
        return os.path.isfile(os.path.join(self.directory, "rclone"))

    def mounted(self):
        try:
            with open("/proc/mounts") as fh:
                for line in fh:
                    if " " + self.mountpoint + " " in line and "fuse" in line:
                        return True
        except Exception:
            pass
        return False

    def snapshot(self):
        with self.lock:
            running = self.proc is not None and self.proc.poll() is None
            if self.proc is not None and self.proc.poll() is not None:
                self.proc = None
                running = False
            mounted = self.mounted()
            if not mounted:
                running = False
            return {
                "available": self.available(),
                "mounted": bool(mounted),
                "enabled": bool(self.cfg.get("enabled") and mounted),
                "host": self.cfg.get("host") or "",
                "share": self.cfg.get("share") or "",
                "folder": self.cfg.get("folder") or "",
                "username": self.cfg.get("username") or "",
                "domain": self.cfg.get("domain") or "",
                "password_set": bool(self.cfg.get("password")),
                "path": "//%s/%s" % (self.cfg.get("host") or "—", self.cfg.get("share") or "—"),
                "mountpoint": self.mountpoint,
                "error": self.error,
                "running": running,
            }

    def apply(self, data):
        data = data or {}
        host = str(data.get("host") or "").strip()
        share = str(data.get("share") or "").strip().strip("/")
        folder = str(data.get("folder") or "").strip().strip("/")
        username = str(data.get("username") or "").strip()
        domain = str(data.get("domain") or "").strip()
        password = data.get("password")
        if host and not HOST_RE.match(host):
            self.error = "server must be a hostname or IP"
            return False
        if share and not SHARE_RE.match(share):
            self.error = "share name is not valid"
            return False
        if folder and (not FOLDER_RE.match(folder) or ".." in folder.split("/")):
            self.error = "folder path is not valid"
            return False
        if username and not USER_RE.match(username):
            self.error = "username is not valid"
            return False
        if domain and not DOMAIN_RE.match(domain):
            self.error = "domain is not valid"
            return False
        with self.lock:
            if host:
                self.cfg["host"] = host
            if "share" in data:
                self.cfg["share"] = share
            if "folder" in data:
                self.cfg["folder"] = folder
            if "username" in data:
                self.cfg["username"] = username
            if "domain" in data:
                self.cfg["domain"] = domain
            if password is not None and password != "":
                self.cfg["password"] = str(password)
            self._save_locked()
            self.error = ""
        return True

    def connect(self):
        if not self.available():
            self.error = "NAS tools missing on this host"
            return False
        with self.lock:
            host = self.cfg.get("host") or ""
            share = self.cfg.get("share") or ""
            if not host or not share:
                self.error = "enter the NAS server and share name"
                return False
            if self.mounted() and self.proc is not None and self.proc.poll() is None:
                self.cfg["enabled"] = True
                self._save_locked()
                self.error = ""
                return True
            self._unmount_locked()
            try:
                self._write_rclone_locked()
            except Exception as exc:
                self.error = str(exc)
                return False
            try:
                os.makedirs(self.mountpoint, exist_ok=True)
            except OSError:
                pass
            env = self._env()
            remote = self._remote_path_locked()
            cmd = [
                os.path.join(self.directory, "rclone"),
                "mount",
                remote,
                self.mountpoint,
                "--config",
                self._conf_path(),
                "--vfs-cache-mode",
                "off",
                "--dir-cache-time",
                "30s",
                "--attr-timeout",
                "2s",
                "--timeout",
                "30s",
                "--contimeout",
                "12s",
                "--uid",
                str(os.getuid()),
                "--gid",
                str(os.getgid()),
                "--allow-other",
                "--log-file",
                "/tmp/gigawatt-rclone.log",
            ]
            try:
                self.proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    env=env,
                    preexec_fn=os.setsid,
                )
            except Exception as exc:
                self.error = str(exc)
                self.proc = None
                return False
            for _ in range(40):
                time.sleep(0.25)
                if self.proc.poll() is not None:
                    err = ""
                    try:
                        with open("/tmp/gigawatt-rclone.log") as fh:
                            lines = fh.read().strip().splitlines()
                        err = lines[-1] if lines else ""
                    except Exception:
                        pass
                    self.error = (err or "mount failed").strip()[:180]
                    self.proc = None
                    return False
                if self.mounted():
                    self.cfg["enabled"] = True
                    self._save_locked()
                    self.error = ""
                    return True
            self._unmount_locked()
            self.error = "NAS did not come online. Check server, share, and password."
            return False

    def disconnect(self):
        with self.lock:
            self.cfg["enabled"] = False
            self._save_locked()
            self._unmount_locked()
            self.error = ""
            return True

    def _remote_path_locked(self):
        share = (self.cfg.get("share") or "").strip("/")
        folder = (self.cfg.get("folder") or "").strip("/")
        path = share
        if folder:
            path = share + "/" + folder
        return "nas:" + path

    def _conf_path(self):
        return os.path.join(self.state_dir, "rclone.conf")

    def _cfg_path(self):
        return os.path.join(self.state_dir, "nas.json")

    def _env(self):
        env = os.environ.copy()
        env["PATH"] = self.directory + ":" + env.get("PATH", "/usr/bin:/bin")
        lib = os.path.join(self.directory, "lib")
        env["LD_LIBRARY_PATH"] = lib + (":" + env["LD_LIBRARY_PATH"] if env.get("LD_LIBRARY_PATH") else "")
        return env

    def _obscure(self, password):
        rclone = os.path.join(self.directory, "rclone")
        out = _run([rclone, "obscure", password or ""], timeout=8, env=self._env())
        line = (out or "").strip().splitlines()
        return line[-1] if line else ""

    def _write_rclone_locked(self):
        obscured = self._obscure(self.cfg.get("password") or "")
        body = (
            "[nas]\n"
            "type = smb\n"
            "host = %s\n"
            "user = %s\n"
            "pass = %s\n"
            "domain = %s\n"
        ) % (
            self.cfg.get("host") or "",
            self.cfg.get("username") or "guest",
            obscured,
            self.cfg.get("domain") or "",
        )
        path = self._conf_path()
        tmp = path + ".tmp"
        with open(tmp, "w") as fh:
            fh.write(body)
        os.replace(tmp, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass

    def _load(self):
        import json
        try:
            with open(self._cfg_path()) as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                for key in self.cfg:
                    if key in data:
                        self.cfg[key] = data[key]
        except Exception:
            pass

    def _save_locked(self):
        import json
        os.makedirs(self.state_dir, exist_ok=True)
        path = self._cfg_path()
        tmp = path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(self.cfg, fh, indent=2)
            fh.write("\n")
        os.replace(tmp, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass

    def _unmount_locked(self):
        proc = self.proc
        self.proc = None
        fuse = os.path.join(self.directory, "fusermount")
        env = self._env()
        if os.path.isfile(fuse):
            subprocess.call([fuse, "-uz", self.mountpoint], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
        subprocess.call(["umount", "-l", self.mountpoint], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if proc is not None:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except Exception:
                try:
                    proc.terminate()
                except Exception:
                    pass
            try:
                proc.wait(timeout=3)
            except Exception:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except Exception:
                    pass

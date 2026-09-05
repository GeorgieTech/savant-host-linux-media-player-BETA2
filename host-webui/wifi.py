#!/usr/bin/env python3
"""Wi-Fi via ConnMan (wlan0). Credentials stay in /var/lib/connman, not git."""
import os
import re
import subprocess
import threading
import time

SERVICE_RE = re.compile(r"^(wifi|ethernet)_[A-Za-z0-9_]+$")
SSID_RE = re.compile(r"^[\x20-\x7e]{1,32}$")
SVC_LINE = re.compile(
    r"^(?P<flags>[*AOIRP ]{0,6})(?P<name>.*?)\s+(?P<path>(?:wifi|ethernet)_[A-Za-z0-9_]+)\s*$"
)
CONFIG_PATH = "/var/lib/connman/gigawatt.config"


def _run(args, timeout=20, input_text=None):
    try:
        return subprocess.run(
            args,
            input=input_text,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            text=True,
        )
    except Exception as exc:
        class _R(object):
            returncode = 1
            stdout = str(exc)

        return _R()


def _ctl(args, timeout=20, input_text=None):
    return _run(["connmanctl"] + list(args), timeout=timeout, input_text=input_text)


class Wifi:
    def __init__(self):
        self.lock = threading.Lock()
        self.error = ""
        self._cache = {"t": 0.0, "networks": []}

    def available(self):
        return os.path.isfile("/usr/bin/connmanctl") and os.path.isdir("/sys/class/net/wlan0")

    def snapshot(self, refresh=False):
        out = {
            "available": self.available(),
            "powered": False,
            "connected": False,
            "ssid": "",
            "ip": "",
            "security": "",
            "service": "",
            "ethernet": False,
            "ethernet_ip": "",
            "error": self.error,
            "networks": [],
        }
        if not out["available"]:
            out["error"] = out["error"] or "Wi-Fi is not available on this host"
            return out
        techs = self._technologies()
        wifi = techs.get("wifi") or {}
        wired = techs.get("ethernet") or {}
        out["powered"] = bool(wifi.get("Powered"))
        out["ethernet"] = bool(wired.get("Connected") or wired.get("Powered") and wired.get("Connected"))
        services = self._services()
        for svc in services:
            if svc["type"] == "ethernet" and svc.get("connected"):
                out["ethernet"] = True
                detail = self._service_detail(svc["id"])
                out["ethernet_ip"] = (detail.get("ipv4") or "").split("/")[0]
            if svc["type"] == "wifi" and svc.get("connected"):
                detail = self._service_detail(svc["id"])
                out["connected"] = True
                out["ssid"] = detail.get("name") or svc.get("name") or ""
                out["ip"] = (detail.get("ipv4") or "").split("/")[0]
                out["security"] = detail.get("security") or svc.get("security") or ""
                out["service"] = svc["id"]
        if refresh:
            out["networks"] = self.scan()
        else:
            out["networks"] = list(self._cache.get("networks") or [])
        out["error"] = self.error
        return out

    def set_powered(self, want):
        if not self.available():
            self.error = "Wi-Fi is not available on this host"
            return False
        cmd = "enable" if want else "disable"
        res = _ctl([cmd, "wifi"], timeout=12)
        text = (res.stdout or "").strip()
        if res.returncode != 0 and "Already" not in text and "enabled" not in text.lower() and "disabled" not in text.lower():
            self.error = (text.splitlines() or ["could not change Wi-Fi power"])[-1][:160]
            return False
        self.error = ""
        return True

    def scan(self):
        if not self.available():
            self.error = "Wi-Fi is not available on this host"
            return []
        techs = self._technologies()
        if not (techs.get("wifi") or {}).get("Powered"):
            self.error = "Wi-Fi is off"
            return []
        _ctl(["scan", "wifi"], timeout=25)
        networks = []
        seen = set()
        for svc in self._services():
            if svc["type"] != "wifi":
                continue
            if svc["id"] in seen:
                continue
            seen.add(svc["id"])
            detail = self._service_detail(svc["id"])
            name = detail.get("name") or svc.get("name") or ""
            networks.append(
                {
                    "id": svc["id"],
                    "name": name,
                    "security": detail.get("security") or svc.get("security") or "",
                    "strength": detail.get("strength") if detail.get("strength") is not None else svc.get("strength"),
                    "connected": bool(detail.get("connected") or svc.get("connected")),
                    "favorite": bool(detail.get("favorite") or svc.get("favorite")),
                    "hidden": not bool(name),
                }
            )
        networks.sort(key=lambda row: (-int(row.get("strength") or 0), (row.get("name") or "").lower()))
        self._cache = {"t": time.time(), "networks": networks}
        self.error = ""
        return networks

    def connect(self, service_id, passphrase=""):
        if not self.available():
            self.error = "Wi-Fi is not available on this host"
            return False
        svc = (service_id or "").strip()
        if not SERVICE_RE.match(svc) or not svc.startswith("wifi_"):
            self.error = "pick a Wi-Fi network from the list"
            return False
        detail = self._service_detail(svc)
        name = detail.get("name") or ""
        security = (detail.get("security") or "").lower()
        if "ieee8021x" in security or "wep" in security:
            self.error = "this network needs a login this host does not support"
            return False
        need_psk = "psk" in security or svc.endswith("_psk")
        if need_psk:
            if len(passphrase) < 8 or len(passphrase) > 63:
                self.error = "Wi-Fi password must be 8–63 characters"
                return False
            if any(ord(ch) < 32 for ch in passphrase) or '"' in passphrase or "\\" in passphrase:
                self.error = "Wi-Fi password has characters this host cannot use"
                return False
        self._write_provision(name, passphrase if need_psk else "", "psk" if need_psk else "none")
        techs = self._technologies()
        if not (techs.get("wifi") or {}).get("Powered"):
            if not self.set_powered(True):
                return False
        if need_psk:
            script = "agent on\nconfig %s --passphrase %s\nconnect %s\nquit\n" % (svc, passphrase, svc)
        else:
            script = "agent on\nconnect %s\nquit\n" % svc
        res = _ctl([], timeout=30, input_text=script)
        text = res.stdout or ""
        low = text.lower()
        time.sleep(1.2)
        snap = self.snapshot()
        if snap.get("connected"):
            self.error = ""
            return True
        if "error" in low or "fail" in low:
            err = [ln.strip() for ln in text.splitlines() if ln.strip()]
            self.error = (err[-1] if err else "could not join Wi-Fi")[:160]
            return False
        # Re-check service state
        detail = self._service_detail(svc)
        if detail.get("connected"):
            self.error = ""
            return True
        self.error = "could not join that network. Check the password."
        return False

    def disconnect(self):
        snap = self.snapshot()
        svc = snap.get("service") or ""
        if not svc:
            self.error = ""
            return True
        res = _ctl(["disconnect", svc], timeout=12)
        text = (res.stdout or "").lower()
        if res.returncode != 0 and "disconnected" not in text and "not connected" not in text:
            self.error = ((res.stdout or "could not disconnect").strip().splitlines() or [""])[-1][:160]
            return False
        self.error = ""
        return True

    def _write_provision(self, ssid, passphrase, security):
        if ssid and not SSID_RE.match(ssid):
            self.error = "network name is not valid"
            return False
        lines = ["[service_gigawatt]", "Type = wifi"]
        if ssid:
            lines.append("Name = %s" % ssid)
        if security == "psk" and passphrase:
            lines.append("Security = psk")
            lines.append("Passphrase = %s" % passphrase)
        elif security == "none":
            lines.append("Security = none")
        lines.append("AutoConnect = true")
        body = "\n".join(lines) + "\n"
        tmp = "/tmp/gigawatt-wifi.config"
        try:
            with open(tmp, "w") as fh:
                fh.write(body)
            os.chmod(tmp, 0o600)
        except OSError as exc:
            self.error = str(exc)
            return False
        try:
            os.replace(tmp, CONFIG_PATH)
            os.chmod(CONFIG_PATH, 0o600)
            return True
        except OSError:
            try:
                os.remove(tmp)
            except OSError:
                pass
            # ConnMan dir is root-owned; join still works via connmanctl config.
            return True

    def _technologies(self):
        res = _ctl(["technologies"], timeout=8)
        techs = {}
        current = None
        for line in (res.stdout or "").splitlines():
            if line.startswith("/net/connman/technology/"):
                current = line.rsplit("/", 1)[-1].strip()
                techs[current] = {}
                continue
            if current is None or "=" not in line:
                continue
            key, val = line.strip().split("=", 1)
            techs[current][key.strip()] = _connman_val(val.strip())
        return techs

    def _services(self):
        res = _ctl(["services"], timeout=8)
        rows = []
        for line in (res.stdout or "").splitlines():
            m = SVC_LINE.match(line.rstrip())
            if not m:
                continue
            flags = (m.group("flags") or "").replace(" ", "")
            path = m.group("path")
            name = (m.group("name") or "").strip()
            kind = "wifi" if path.startswith("wifi_") else "ethernet"
            security = ""
            if path.endswith("_psk"):
                security = "psk"
            elif path.endswith("_ieee8021x"):
                security = "ieee8021x"
            elif path.endswith("_none") or path.endswith("_open"):
                security = "none"
            rows.append(
                {
                    "id": path,
                    "name": name,
                    "type": kind,
                    "security": security,
                    "connected": ("O" in flags or "R" in flags),
                    "favorite": "*" in flags,
                    "strength": None,
                }
            )
        return rows

    def _service_detail(self, svc):
        if not SERVICE_RE.match(svc or ""):
            return {}
        res = _ctl(["services", svc], timeout=8)
        info = {"id": svc, "connected": False, "favorite": False, "strength": None, "name": "", "security": "", "ipv4": ""}
        for line in (res.stdout or "").splitlines():
            if "=" not in line:
                continue
            key, val = line.strip().split("=", 1)
            key = key.strip()
            val = val.strip()
            if key == "Name":
                info["name"] = val
            elif key == "State":
                info["connected"] = val in ("ready", "online")
            elif key == "Favorite":
                info["favorite"] = _connman_val(val) is True
            elif key == "Strength":
                try:
                    info["strength"] = int(val)
                except ValueError:
                    info["strength"] = None
            elif key == "Security":
                info["security"] = val.strip("[] ").split(",")[0].strip()
            elif key == "IPv4":
                # [ Method=dhcp, Address=192.168.1.20, Netmask=... ]
                m = re.search(r"Address=([0-9.]+)", val)
                if m:
                    info["ipv4"] = m.group(1)
        return info


def _connman_val(val):
    if val in ("True", "true", "yes"):
        return True
    if val in ("False", "false", "no"):
        return False
    return val

# Deploy notes — Gigawatt V0.1

Target: **192.168.1.178**. Never **192.168.1.40**. Never **192.168.1.180** (Beta1).

SSH user: `RPM`. Do not commit the password. `scp -O` from modern macOS.

## What V0.1 did on this host

1. Masked `savant-startup-manager.service` and set default target to `multi-user.target`.
2. Stopped and masked `nginx.service` so port 80 is free.
3. Installed `host-webui.service`, files in `/data/www`.
4. Created `/data/music` and `/data/gigawatt`.

Savant images on the eMMC are not deleted.

## Copy the web UI

From the repo root:

```sh
scp -O host-webui/index.html host-webui/server.py host-webui/hostplayer.py \
  host-webui/airplay.py host-webui/dlna.py host-webui/spotify.py host-webui/nas.py \
  host-webui/pin-hostname.sh host-webui/gigawatt-hostname.service \
  host-webui/gigawatt-pulse.service host-webui/host-webui.service \
  RPM@192.168.1.178:/tmp/
# Spotify binary (once, or after a go-librespot bump):
# scp -O -r host-webui/spotify RPM@192.168.1.178:/tmp/spotify
ssh RPM@192.168.1.178
sudo -n /usr/bin/env bash -c '
  cp /tmp/index.html /tmp/server.py /tmp/hostplayer.py /tmp/airplay.py \
    /tmp/dlna.py /tmp/spotify.py /tmp/nas.py /tmp/pin-hostname.sh /data/www/
  chmod +x /data/www/pin-hostname.sh
  cp /tmp/host-webui.service /tmp/gigawatt-hostname.service /tmp/gigawatt-pulse.service \
    /etc/systemd/system/
  systemctl daemon-reload
  systemctl enable gigawatt-hostname.service gigawatt-pulse.service host-webui.service
  systemctl restart gigawatt-hostname.service gigawatt-pulse.service host-webui.service
'
```

Open http://192.168.1.178/

## First-time unit (already done for V0.1)

```sh
scp -O host-webui/index.html host-webui/server.py host-webui/host-webui.service RPM@192.168.1.178:/tmp/
# as root via sudo env:
mkdir -p /data/www /data/music /data/gigawatt
chown RPM:RPM /data/www /data/music /data/gigawatt
cp /tmp/index.html /tmp/server.py /data/www/
cp /tmp/host-webui.service /etc/systemd/system/host-webui.service
systemctl mask savant-startup-manager.service nginx.service
systemctl stop savant-startup-manager.service nginx.service
systemctl set-default multi-user.target
systemctl daemon-reload
systemctl enable --now host-webui.service
```

## Constraints

- No `apt`. Yocto image.
- Python 3.8 stdlib only.
- Accounts file is `/data/gigawatt/users.json` — not in git.
- Port 80 is the UI.

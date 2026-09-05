# Host hardware — 192.168.1.178 (Gigawatt Beta2)

Factory product: **Savant Smart Host with Control, SHC-2000-00**. Sister of the Beta1 box at 192.168.1.180.

Do **not** use **192.168.1.40**. Do not deploy this tree to **192.168.1.180**.

## Identity

| Field | Value |
|---|---|
| IP | 192.168.1.178/24 |
| Hostname | `GWH-001aae0733360000` (factory was `sav-001aae0733360000`) |
| Device tree model | `SHC-S2-00` |
| Userspace | 32-bit `armv7l` |
| Serial (from Beta1 notes) | `QSH180100203` |

## Machine (same class as Beta1)

- i.MX6 Quad, 4× Cortex-A9
- ~2.0 GB RAM, no swap
- `/data` on `mmcblk0p3` ≈ 3.1 GB (headroom for a music library)
- Ethernet `eth0` at 192.168.1.178
- Audio: Pulse sink `alsa_output.platform-sound-spdif.stereo-fallback` (TOSLINK / `imx-spdif`, 24-bit 96 kHz reported)

## Software image (as found)

- Kernel 4.14.78
- Savant Embedded Linux 20.04, build **695**
- Python **3.8.17**
- ffmpeg 4.2.2, paplay / PulseAudio
- nginx was on **port 80 / 443** while Savant was live
- SSH user `RPM` (password not stored in this repo)
- `sudo` NOPASSWD includes `/usr/bin/env` and `/bin/systemctl`

Savant `startupManager` is **masked** for Gigawatt. Default target is `multi-user.target`. nginx is stopped/masked so port 80 is the Python UI.

The factory hostname was `sav-` plus the unit id. `savant-init.service` still writes `sav-<uid>` into `/etc/hostname` on every boot. Gigawatt pins `GWH-<uid>` afterward via `gigawatt-hostname.service` (`/data/www/pin-hostname.sh`). Keep Savant’s launcher masked.

PulseAudio is the TOSLINK path. The stock `pulseaudio.service` is disabled and tied to a user socket, so a power cut left no `/var/run/pulse/native` and AirPlay failed to restore. `gigawatt-pulse.service` starts the system daemon (`savant-vcd.pa`) before the web UI.

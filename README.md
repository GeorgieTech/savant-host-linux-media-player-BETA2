# Gigawatt Beta2

Repo: [`savant-host-linux-media-player-BETA2`](https://github.com/GeorgieTech/savant-host-linux-media-player-BETA2).

**Gigawatt** is a local LAN music server on a recycled Savant SHC-2000. Same job as [Beta1 / Giggwatt](https://github.com/GeorgieTech/savant-host-linix-media-player): play **MP3, FLAC, and Opus** from disk on this host. New name, new UI, new box.

This project is **not affiliated with Savant Systems**.

**Do not use 192.168.1.40** (live Carrillos Resident Savant). Beta1 stays on **192.168.1.180**. This tree is **192.168.1.178** only.

## Current status — V0.4

Target: **192.168.1.178** (`GWH-001aae0733360000`)

Live UI: [http://192.168.1.178/](http://192.168.1.178/)

V0.2 adds **browser playback and a now-playing visual**. Optical TOSLINK is still later.

| Piece | State |
|---|---|
| Name | **Gigawatt** (not Giggwatt) |
| Look | Basic modern web-app UI (not the Beta1 time-circuit HUD) |
| First visit | Create the first local account |
| Later visits | Sign in |
| After sign-in | Home, Library (play / manage / upload), EQ, Settings, live player dock |
| Playback | This browser, from `/data/music` |
| Formats | MP3, FLAC, Opus |
| Code on host | `/data/www` |
| Accounts | `/data/gigawatt/users.json` (hashed, not in git) |
| Library dir | `/data/music` (empty for now) |

## Host

SHC-2000 sister unit: i.MX6 Quad, TOSLINK S/PDIF, Python 3.8, ffmpeg 4.2.2, PulseAudio. Savant launcher is masked so port 80 is the app.

- Hardware: [docs/HOST.md](docs/HOST.md)
- How it was put on the box: [docs/DEPLOY.md](docs/DEPLOY.md)
- Shape of V0.1: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)

## License

[MIT](LICENSE) © 2026 George Carrillo.

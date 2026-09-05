# Changelog

## V0.2 — browser playback + visualizer

- Library lists MP3 / FLAC / Opus on `/data/music`
- Play in this browser (`/api/media` with Range)
- Home stage visualizer follows the track
- First track: *Eulogy — Stranger Things 2 Soundtrack*

## After V0.1

- Host renamed to `GWH-001aae0733360000` (`/etc/hostname`, `/etc/hosts`, systemd static hostname). IP stays 192.168.1.178.

## V0.1 — web app + accounts (192.168.1.178)

- Named **Gigawatt**
- Modern web-app UI (home, library shelf, settings, player dock)
- First visit creates a local account; later visits sign in
- Playback not wired yet (MP3 / FLAC / Opus is the goal)
- Savant launcher and nginx masked so port 80 is this app

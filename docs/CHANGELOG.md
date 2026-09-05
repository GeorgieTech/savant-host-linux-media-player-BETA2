# Changelog

## Spotify Connect fixes (after V0.8)

- Idle Connect no longer looks “playing” (empty /status 204 was locking the UI)
- go-librespot uses the Pulse backend, not ALSA device `pulse`
- Finds the binary next to the UI if `/data/opt/spotify` is missing
- Start failures write a log and show the last lines in Settings

## V0.8 — NAS SMB music folder

- Settings: connect an SMB share (server, share, folder, user, password)
- When mounted, a **NAS Music** page lists the share (separate from local Library)
- Kernel has no CIFS; rclone FUSE mounts at `/data/nas`

## V0.7 — DLNA renderer + Spotify Connect

- Settings: **DLNA** On/Off and advertise name (BubbleUPnP, VLC, Windows Cast to Device)
- Settings: **Spotify Connect** On/Off and advertise name (`go-librespot`, Premium, internet)
- Both play TOSLINK, show now-playing on Home, and take over from the library like AirPlay

## V0.6 — AirPlay name and TOSLINK routing

- Settings: custom AirPlay name (default Gigawatt)
- Settings: audio out **Browser** or **TOSLINK**, and it actually routes
- AirPlay still plays TOSLINK; library playback follows the output setting
- Settings grouped like the rest of the app (Account / Playback / AirPlay)
- Library: upload progress bar (bytes sent, file name, n of n) while tracks copy to the host

## V0.5 — AirPlay 1

- Settings: AirPlay On/Off. Host advertises as **Gigawatt**
- iPhone/Mac audio plays TOSLINK via Pulse
- Local browser playback stops when an AirPlay session begins
- Home and the player bar show the AirPlay title / artist / album

## V0.4 — volume + EQ

- Bottom player: Volume slider (0–100), same type as Beta1
- New **EQ** page: 10-band equalizer for this-browser playback
- EQ presets: Flat, Harman loudspeaker, B&K 1974, Optimum HiFi, NAD / Bluesound
- Volume and EQ persist on the host

## V0.3 — library manage + upload

- Library header: **Upload tracks** and **Manage**
- Manage: set genre or delete a file on the host
- Upload (picker or drop) onto `/data/music`

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

# Changelog

## V0.14 — library browse, art, lyrics

- Library page: Artists / Albums / Tracks, search, and breadcrumbs like NAS Music
- Titles, artists, and albums come from file tags, then folders / `Artist - Title` filenames
- Cover art from embedded pictures or `cover.jpg` / `folder.jpg`; iTunes lookup if missing
- Live lyrics on Now Playing from `.lrc` / tags, then lrclib.net when the file has none
- Next/prev follows the album or search list you started from

## V0.13 — hostname pin and AirPlay restore

- Pin hostname to `GWH-<uid>` after `savant-init` (which resets `sav-<uid>` on every boot)
- Start system PulseAudio at boot so TOSLINK / AirPlay / Spotify have `/var/run/pulse/native`
- AirPlay On is restored after power loss; shairport retries until Pulse is up
- Settings AirPlay toggle follows the saved On/Off even while the receiver is starting
- AirPlay stays **AirPlay 1** (`shairport-sync` 3.3.7). AirPlay 2 is not on this Yocto image

## V0.12 — host reset and password

- Settings: **Reboot host** (`POST /api/host/reboot`) — does not write account, EQ, volume, names, or NAS
- Settings: change password for the signed-in user (`POST /api/auth/password`) — library and host settings stay put
- Sessions stay in RAM, so a reboot or power loss still requires sign-in

## V0.11 — i.MX6 performance

- Sign-in and TOSLINK skip no longer ffprobe every library file
- `/api/now` reuses host / RAM / disk for 5 seconds
- Poll `/api/now` every 1s only while TOSLINK / AirPlay / Spotify / DLNA is live; 5–8s when idle
- Visualizer rAF runs only on Now Playing (about 10 fps when idle)
- Library ffprobe cache is LRU and persisted in `/data/gigawatt/library-meta.json`
- NAS rclone mount uses `--vfs-cache-mode writes` with a 256 MB cap
- EQ sliders apply to TOSLINK on release / leaving the page, not on every drag
- Notes: `docs/V010_DEBUG_NOTES.txt`, `docs/PERFORMANCE_RECOMMENDATIONS.txt`

## V0.10 — TOSLINK EQ

- DLNA: GetCurrentTransportActions, UPnP SOAP faults, UPnP HTTP SERVER header, HTTPS rejected with a clear error, ffmpeg play errors surfaced
- The 10-band EQ page applies to host TOSLINK as well as this browser (library, NAS, DLNA)
- ffmpeg `lowshelf` / `equalizer` / `highshelf` match the on-page bands (Q 1.1)
- Dragging a band while TOSLINK is playing reapplies after a short pause; AirPlay and Spotify still play the optical jack without these bands

## V0.9 — NAS Music by Artist / Albums / Tracks

- NAS Music page: toggle **Artists**, **Albums**, and **Tracks**
- Browse the share by folder (artist → album → tracks) instead of flattening the whole tree
- Search filters the current view; letter jump on long artist lists
- Connect no longer walks every file (that stalled FUSE on large shares)
- Library and NAS Music stay on the current page when you tap a track; **Now Playing** is only for the visualizer
- The Home page is renamed **Now Playing**
- Spotify Connect: empty `/status` is idle (not “playing”); Pulse backend; log tail on failed start

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

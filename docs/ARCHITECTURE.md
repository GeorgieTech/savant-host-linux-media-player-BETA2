# Architecture — Gigawatt V0.2

V0.2 is the **web app, local accounts, library, and browser playback**. Optical TOSLINK is later. Formats: **MP3, FLAC, and Opus**.

## One process

`host-webui/server.py` binds `0.0.0.0:80` and serves the single-page UI.

```
browser  --HTTP-->  python3 server.py :80
                         |
                         +-- GET  /                 app (login or shell)
                         +-- GET  /api/auth/status  setup vs signed-in, library
                         +-- POST /api/auth/setup   first account
                         +-- POST /api/auth/login
                         +-- POST /api/auth/logout
                         +-- GET  /api/library
                         +-- GET  /api/media?name=  file stream (Range) for this browser
```

Playback is HTML5 audio in the page that opened the UI. A Web Audio analyser drives the Home visualizer. No ffmpeg child yet. No AirPlay.

## Accounts

First visit with an empty user file shows **Create account**. After that, **Sign in**.

- File: `/data/gigawatt/users.json`
- Password: PBKDF2-HMAC-SHA256 (80k rounds), random salt
- Session: HttpOnly cookie `gigawatt_session`, random token in process memory (signing out or restarting the service ends the session)

This is a LAN appliance account. Nothing is sent to the cloud.

## UI

Modern web-app chrome, not the Beta1 time-circuit HUD.

- Gate: create account or sign in
- App: top bar, side nav (Home / Library / Settings), bottom player dock
- Home: now-playing visualizer
- Library: tap to play
- Player dock: play / pause / next / seek

Name: **Gigawatt**.

## Later (not V0.1)

Queue, decode, Pulse/TOSLINK, browser output, upload. Keep one process when those land.

# Architecture — Gigawatt V0.1

V0.1 is the **web app and local accounts**. It does not play audio yet. The goal after this is the same as Beta1: a music server for **MP3, FLAC, and Opus** on this host, optical TOSLINK later.

## One process

`host-webui/server.py` binds `0.0.0.0:80` and serves the single-page UI.

```
browser  --HTTP-->  python3 server.py :80
                         |
                         +-- GET  /                 app (login or shell)
                         +-- GET  /api/auth/status  setup vs signed-in
                         +-- POST /api/auth/setup   first account
                         +-- POST /api/auth/login
                         +-- POST /api/auth/logout
```

No ffmpeg child in V0.1. No AirPlay. No second language.

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
- Library is an empty shelf on purpose
- Player controls are visible and disabled

Name: **Gigawatt**.

## Later (not V0.1)

Queue, decode, Pulse/TOSLINK, browser output, upload. Keep one process when those land.

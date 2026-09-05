# host-webui (Gigawatt V0.8)

Python 3.8 stdlib server plus one HTML page. Runs on **192.168.1.178:80**.

- `GET /` — login / create account, then the app
- `GET /api/auth/status` — `{setup_required, user, host, version, formats, playback, tracks}`
- `POST /api/auth/setup` — `{username, password, confirm}` (only when no users exist)
- `POST /api/auth/login` — `{username, password}`
- `POST /api/auth/logout`
- `GET /api/library` — tracks in `/data/music` (signed in)
- `GET /api/media?name=` — audio stream with HTTP Range (signed in)
- `POST /api/upload` — multipart `file` fields into `/data/music`
- `POST /api/volume` — `{volume}` 0–100
- `POST /api/eq` — `{eq: [10 gains in dB]}`
- `GET /api/now` — output, host TOSLINK player, AirPlay snapshot
- `POST /api/output` — `{output: "browser"|"optical"}`
- `POST /api/play` `{name, start?}` / `/api/pause` / `/api/stop` / `/api/seek` — TOSLINK transport
- `GET /api/airplay` — same payload as `/api/now`
- `POST /api/airplay` — `{enabled}` and/or `{name}`
- `GET/POST /api/dlna` — `{enabled}` and/or `{name}` (UPnP MediaRenderer on UDP 1900 + TCP 49494)
- `GET/POST /api/spotify` — `{enabled}` and/or `{name}` (Spotify Connect, Premium)
- `GET/POST /api/nas` — SMB connect `{host, share, folder, username, password, domain, enabled}`
- `GET /api/nas/library` — tracks on the mounted share
- `POST /api/library/save` — `{tags: {name: genre}, delete: [name]}` after Manage
- `POST /api/tag` — `{name, genre}` (empty genre clears)
- `POST /api/delete` — `{name}`

Copy the Python modules and `index.html` to `/data/www`. Spotify Connect also needs
`host-webui/spotify/go-librespot` at `/data/opt/spotify` (or next to `spotify.py`).

# host-webui (Gigawatt V0.2)

Python 3.8 stdlib server plus one HTML page. Runs on **192.168.1.178:80**.

- `GET /` — login / create account, then the app
- `GET /api/auth/status` — `{setup_required, user, host, version, formats, playback, tracks}`
- `POST /api/auth/setup` — `{username, password, confirm}` (only when no users exist)
- `POST /api/auth/login` — `{username, password}`
- `POST /api/auth/logout`
- `GET /api/library` — tracks in `/data/music` (signed in)
- `GET /api/media?name=` — audio stream with HTTP Range (signed in)

Copy `index.html` and `server.py` to `/data/www`.

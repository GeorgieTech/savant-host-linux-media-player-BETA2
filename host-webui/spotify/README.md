# Spotify Connect runtime (Gigawatt)

armv7hf `go-librespot` **v0.9.0** (`linux_armv6` build). Native **pulseaudio** backend into Pulse (`PULSE_SERVER`), then TOSLINK. Do not use ALSA device `pulse` — this Yocto image has Pulse, not the ALSA pulse plugin.

Installed on the host as `/data/opt/spotify`. Settings starts and stops it.

Advertise name defaults to **Gigawatt**. Credentials stay in `/data/gigawatt/spotify` (not in git).

Spotify Premium is required. Local API is `127.0.0.1:3678`.

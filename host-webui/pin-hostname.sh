#!/bin/bash
# Pin Gigawatt hostname after savant-init.sh (which forces sav-<uid> on every boot).
set -e
UID_FILE=/run/savant/uid
FALLBACK=001aae0733360000
uid=$(tr -d '[:space:]' < "$UID_FILE" 2>/dev/null || true)
if [ ${#uid} -ne 16 ]; then
  uid=$FALLBACK
fi
name="GWH-${uid}"
echo "$name" > /etc/hostname
sysctl -w kernel.hostname="$name" >/dev/null
if command -v hostname >/dev/null 2>&1; then
  hostname "$name" >/dev/null 2>&1 || true
fi
if ! grep -qE "127\\.0\\.1\\.1[[:space:]]+${name}([[:space:]]|$)" /etc/hosts; then
  echo -e "127.0.1.1\t${name}" >> /etc/hosts
fi

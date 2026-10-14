#!/bin/sh
# Creates manager configuration only. Never creates or changes commands.json.
set -eu
cd "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
if [ "$#" -ne 1 ]; then
  echo 'Usage: sh configure.sh <NAS-LAN-IPv4>' >&2
  exit 1
fi
ip=$1
if ! printf '%s\n' "$ip" | awk -F. '
  NF != 4 { exit 1 }
  { for (i=1;i<=4;i++) if ($i !~ /^[0-9]+$/ || $i > 255) exit 1
    if ($1 == 10 || ($1 == 172 && $2 >= 16 && $2 <= 31) || ($1 == 192 && $2 == 168)) exit 0
    exit 1
  }'; then
  echo 'Use a private IPv4 address: 10.x.x.x, 172.16-31.x.x, or 192.168.x.x.' >&2
  exit 1
fi
skills=/volume2/ssd_docker/xiaogpt/config/skills
if [ ! -f "$skills/commands.json" ] || [ -L "$skills/commands.json" ]; then
  echo "Missing regular file: $skills/commands.json (nothing was changed)." >&2
  exit 1
fi
if [ -e .env ] || [ -L .env ]; then
  echo '.env already exists; edit it manually. Nothing was overwritten.' >&2
  exit 1
fi
password=$(od -An -N24 -tx1 /dev/urandom | tr -d ' \n')
[ ${#password} -eq 48 ] || { echo 'Password generation failed.' >&2; exit 1; }
umask 077
set -C
{
  printf 'NAS_LAN_IP=%s\n' "$ip"
  printf 'APP_PASSWORD=%s\n' "$password"
} > .env
printf 'Ready. Existing commands.json was not changed.\n'
printf 'URL: http://%s:9528\n' "$ip"
printf 'Login password: %s\n' "$password"
printf 'Start: docker compose up -d --build\n'

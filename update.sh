#!/bin/sh
# Update this manager only. Never initializes commands.json or rewrites .env.
set -eu
cd "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"

fail() { printf '\n[ERROR] %s\n' "$1" >&2; exit 1; }
command -v docker >/dev/null 2>&1 || fail 'Docker is unavailable.'
[ -d .git ] || fail 'Run this script from the cloned xiaoai-commands project.'
[ -f .env ] || fail '.env is missing. Configure the manager before updating.'
[ -f docker-compose.yml ] || fail 'docker-compose.yml is missing.'
docker compose version >/dev/null 2>&1 || fail 'Docker Compose v2 is required.'

# The NAS already has this image; never try downloading another updater image.
docker image inspect alpine/git >/dev/null 2>&1 || fail 'Cached alpine/git image is missing.'
proxy=${XIAOAI_UPDATE_PROXY:-http://192.168.31.192:34421}

printf '\n[1/3] Pulling code through the NAS proxy (maximum 60 seconds)...\n'
if ! docker run --rm --pull=never \
  -e XIAOAI_GIT_PROXY="$proxy" \
  -v "$PWD:/repo" -w /repo --entrypoint sh alpine/git \
  -c 'timeout 60 git -c "http.proxy=$XIAOAI_GIT_PROXY" pull --ff-only'; then
  fail 'Pull failed or timed out. No build was started; the running manager was not stopped. Check the error above.'
fi

printf '\n[2/3] Building and starting the manager...\n'
if ! docker compose up -d --build --pull never manager; then
  fail 'Build/start failed. Check the error above; commands.json and .env were not rewritten by this script.'
fi

printf '\n[3/3] Container status\n'
docker compose ps -a manager
printf '\nUpdate command finished. Refresh http://192.168.31.192:9528/\n'
printf 'If the status is Restarting or unhealthy, run: docker compose logs --tail=50 manager\n'

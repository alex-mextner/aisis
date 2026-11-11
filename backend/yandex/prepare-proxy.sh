#!/usr/bin/env bash
set -euo pipefail
pin=3b902d61b85642770cd96ed76ffaf80832c99289
dest=${AISIS_PROXY_SOURCE:-"${XDG_CACHE_HOME:-$HOME/.cache}/aisis-yandex/https-proxy"}
mkdir -p -- "$(dirname -- "$dest")"
fresh_clone=0
if [[ ! -d "$dest/.git" ]]; then
  git clone --no-checkout https://github.com/v3rt3p/https-proxy.git "$dest"
  fresh_clone=1
fi
if [[ "$fresh_clone" == 0 && -n "$(git -C "$dest" status --porcelain)" ]]; then
  echo 'Proxy cache has local changes; refusing overwrite.' >&2
  exit 1
fi
git -C "$dest" fetch origin "$pin"
git -C "$dest" checkout --detach "$pin"
printf 'AISIS_PROXY_SOURCE=%s\n' "$dest"

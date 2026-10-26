#!/usr/bin/env bash
# Optional fallback only. Inspect/reuse existing host speech resources first.
set -euo pipefail
pin=e1770f19d67fe31908c5649cc680ad8ade77ed72
dest=${AISIS_STT_SOURCE:-"${XDG_CACHE_HOME:-$HOME/.cache}/aisis-yandex/quasistreaming-gigam"}
mkdir -p -- "$(dirname -- "$dest")"
fresh_clone=0
if [[ ! -d "$dest/.git" ]]; then
  git clone --no-checkout https://github.com/v3rt3p/quasistreaming-gigam.git "$dest"
  fresh_clone=1
fi
if [[ "$fresh_clone" == 0 && -n "$(git -C "$dest" status --porcelain)" ]]; then
  echo 'STT cache has local changes; refusing overwrite.' >&2
  exit 1
fi
git -C "$dest" fetch origin "$pin"
git -C "$dest" checkout --detach "$pin"
printf 'AISIS_STT_SOURCE=%s\n' "$dest"

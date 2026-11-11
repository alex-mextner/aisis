#!/usr/bin/env bash
set -euo pipefail
pin=a983cda873f80be66d820bffe01e09f125f62cf8
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
source_dir=${AISIS_QUASAR_SOURCE:-"${XDG_CACHE_HOME:-$HOME/.cache}/aisis-yandex/quasar-server"}
mkdir -p -- "$(dirname -- "$source_dir")"
fresh_clone=0
if [[ ! -d "$source_dir/.git" ]]; then
  git clone --no-checkout https://github.com/v3rt3p/quasar-server.git "$source_dir"
  fresh_clone=1
fi
if [[ "$fresh_clone" == 0 && -n "$(git -C "$source_dir" status --porcelain)" ]]; then
  echo 'Upstream cache contains changes; choose a new AISIS_QUASAR_SOURCE to rebuild.' >&2
  exit 1
fi
git -C "$source_dir" fetch origin "$pin"
git -C "$source_dir" checkout --detach "$pin"
python3 "$script_dir/patch_quasar.py" "$source_dir"
printf 'AISIS_QUASAR_SOURCE=%s\n' "$source_dir"

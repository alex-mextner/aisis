#!/usr/bin/env bash
# Install AISIS backend files. Does not alter speaker, router or browser state.
set -euo pipefail
src=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
dest="$HOME/.local/share/aisis-yandex"
config="$HOME/.config/aisis-yandex"
state="$HOME/.local/state/aisis-yandex"
runtime_files=(adapter.py chatgpt_pm.py pm_cdp_server.py stt_wyoming.py operate.py compose.yaml requirements.txt)
units=(aisis-yandex-adapter.service aisis-yandex-pm.service aisis-yandex-stt.service aisis-yandex-wyoming-tunnel.service)
for file in "${runtime_files[@]}" "${units[@]}" runtime.env.example; do
  if [[ ! -f "$src/$file" ]]; then
    printf 'Required source file is missing: %s\n' "$file" >&2
    exit 1
  fi
done
command -v uv >/dev/null || { printf 'uv is required for installation.\n' >&2; exit 1; }
install -d -m700 "$dest" "$config" "$state" "$HOME/.config/systemd/user"
for file in "${runtime_files[@]}"; do
  install -m600 "$src/$file" "$dest/$file"
done
if [[ ! -x "$dest/venv/bin/python" ]]; then
  uv venv "$dest/venv" --python 3.12
fi
uv pip install --python "$dest/venv/bin/python" -r "$dest/requirements.txt"
# Reuse the existing browser libraries without modifying their shared venv.
guardian_python="$HOME/.local/share/agent-tools/chatgpt-guardian-venv/bin/python"
if [[ ! -x "$guardian_python" ]]; then
  echo 'Existing pm Guardian Python environment is required.' >&2
  exit 1
fi
uv pip install --python "$guardian_python" --target "$dest/pm-deps" 'aiohttp==3.13.3'
for unit in "${units[@]}"; do
  install -m600 "$src/$unit" "$HOME/.config/systemd/user/$unit"
done
if [[ ! -f "$config/runtime.env" ]]; then
  python3 - "$src/runtime.env.example" "$config/runtime.env" <<'PY'
import os,pathlib,secrets,sys
text=pathlib.Path(sys.argv[1]).read_text().replace('/home/USER',str(pathlib.Path.home()))
text=text.replace('REPLACE_WITH_RANDOM_HEX',secrets.token_hex(24)).replace('REPLACE_WITH_PERSISTENT_RANDOM_HEX',secrets.token_hex(32)).replace('REPLACE_WITH_PRIVATE_RANDOM_TOKEN',secrets.token_urlsafe(32))
p=pathlib.Path(sys.argv[2]);p.write_text(text);p.chmod(0o600)
PY
fi
if [[ ! -f "$state/deployment-ledger.md" ]]; then
cat > "$state/deployment-ledger.md" <<'MD'
# AISIS deployment ledger

- Worktree: yandex-chatgpt-backend, feature branch; public sources contain no host inventory.
- Quasar source pinned a983cda873f80be66d820bffe01e09f125f62cf8.
- Loopback adapter 31117, admin 31116, Postgres 31119; speaker protocol 31115.
- Adapter source installed. Service must be started only after real browser and voice credentials are configured.
- No speaker/router/DNS mutation was made by installation.
- Outstanding: real ChatGPT continuity, Fish Opus, streaming STT, runtime launch, exact speaker compatibility, one-device end-to-end migration.
MD
fi
printf '\n- Adapter files installed/updated at %s UTC.\n' "$(date -u +%FT%T)" >> "$state/deployment-ledger.md"
systemctl --user daemon-reload
printf 'Adapter installed; private configuration and deployment ledger: %s\n' "$state"

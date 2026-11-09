#!/usr/bin/env bash
# Regenerates the Whisper initial prompt (voice_vocab.py prompt: commands, device/room names from HA, favourite artists)
# into an EnvironmentFile read by wyoming-whisper.service; restarts the recognizer only when the prompt changed.
set -euo pipefail
data="$HOME/.local/share/aisis"
code="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
envfile="$HOME/.config/aisis/whisper-prompt.env"
prompt=$("$data/venv/bin/python" "$code/voice_vocab.py" prompt)
[[ -n $prompt ]] || { echo "empty prompt, keeping the previous one" >&2; exit 1; }
prompt=${prompt//\\/\\\\}; prompt=${prompt//\"/\\\"}
new=$(printf 'WHISPER_INITIAL_PROMPT="%s"\n' "${prompt//$'\n'/ }")
mkdir -p -- "$(dirname -- "$envfile")"
if [[ -f $envfile && $(<"$envfile") == "$new" ]]; then exit 0; fi
tmp=$(mktemp "$envfile.XXXXXX")
printf '%s\n' "$new" >"$tmp"; chmod 600 "$tmp"; mv -f -- "$tmp" "$envfile"
systemctl --user try-restart wyoming-whisper.service

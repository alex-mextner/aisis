#!/usr/bin/env bash
# Stage an immutable Claude-primary release directory. Touches no service, unit, env file or key.
# Usage: install-claude-primary.sh [RELEASE_ID]   (default: claude-primary-<UTC timestamp>)
# Prints the release directory. Activation (claude-primary.env + adapter drop-in) is in README.md.
set -euo pipefail
src=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
dest="$HOME/.local/share/aisis-yandex"
id=${1:-claude-primary-$(date -u +%Y%m%dT%H%M%SZ)}
[[ $id =~ ^claude-primary-[A-Za-z0-9._-]{1,64}$ ]] || { printf 'Invalid release id: %s\n' "$id" >&2; exit 1; }
release="$dest/releases/$id"
for file in claude_primary.py chatgpt_pm.py; do
  [[ -f "$src/$file" ]] || { printf 'Required source file is missing: %s\n' "$file" >&2; exit 1; }
done
# The fallback imports chatgpt_pm.py from this directory; it must be the exact bytes the
# running PM client contract was reviewed with.
if [[ -f "$dest/chatgpt_pm.py" ]] && ! cmp -s "$src/chatgpt_pm.py" "$dest/chatgpt_pm.py"; then
  printf 'Refusing: installed chatgpt_pm.py differs from the source copy.\n' >&2
  exit 1
fi
[[ ! -e "$release" ]] || { printf 'Release already exists: %s\n' "$release" >&2; exit 1; }
install -d -m700 "$dest/releases"
staging=$(mktemp -d "$dest/releases/.staging.XXXXXX")
trap 'chmod -R u+w -- "$staging" 2>/dev/null; rm -rf -- "$staging"' EXIT
for file in claude_primary.py chatgpt_pm.py README.md claude-primary.env.example claude-primary.conf.example claude-sources.example.json; do
  install -m400 "$src/$file" "$staging/$file"
done
(cd "$staging" && sha256sum -- * > MANIFEST && chmod 400 MANIFEST)
chmod 500 "$staging"
mv -T -- "$staging" "$release"
trap - EXIT
printf '%s\n' "$release"

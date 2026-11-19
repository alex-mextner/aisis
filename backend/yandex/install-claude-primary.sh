#!/usr/bin/env bash
# Stage an immutable release. Activation is separate; no service, env file or key is changed.
# Usage: install-claude-primary.sh [RELEASE_ID] [ABSOLUTE_INSTALL_ROOT] [CURRENT_FALLBACK_FILE]
set -euo pipefail
umask 077
log=$(mktemp "/tmp/aisis-claude-install-$(date -u +%Y%m%dT%H%M%SZ)-XXXXXX.log")
exec 3>>"$log"
printf 'log=%s\n' "$log" >&2
started=$SECONDS
staging=''
finish() {
  result=$?
  trap - EXIT
  if [[ -n $staging && -d $staging ]]; then
    chmod -R u+w -- "$staging"
    rm -rf -- "$staging"
  fi
  printf 'stage=finish exit=%s elapsed_s=%s\n' "$result" "$((SECONDS-started))" >&3
  printf 'log=%s exit=%s\n' "$log" "$result" >&2
  exit "$result"
}
trap finish EXIT
printf 'stage=start\n' >&3
src=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
dest=${2:-$HOME/.local/share/aisis-yandex}
id=${1:-claude-primary-$(date -u +%Y%m%dT%H%M%SZ)}
[[ $dest = /* ]] || { printf 'Install root must be absolute.\n' >&2; exit 1; }
[[ $id =~ ^claude-primary-[A-Za-z0-9._-]{1,64}$ ]] || { printf 'Invalid release id.\n' >&2; exit 1; }
[[ $# -le 3 ]] || { printf "Too many arguments.\\n" >&2; exit 1; }
reference=${3:-$dest/chatgpt_pm.py}
if [[ $# -ge 2 && ( $reference != /* || ! -f $reference ) ]]; then
  printf "An explicit install root requires the current absolute fallback-client file.\\n" >&2
  exit 1
fi
release="$dest/releases/$id"
for file in claude_primary.py chatgpt_pm.py; do
  [[ -f "$src/$file" ]] || { printf 'Required source file is missing: %s\n' "$file" >&2; exit 1; }
done
# Keep the exact reviewed fallback-client contract.
if [[ -f "$reference" ]] && ! cmp -s "$src/chatgpt_pm.py" "$reference"; then
  printf 'Refusing: installed chatgpt_pm.py differs from the source copy.\n' >&2
  exit 1
fi
[[ ! -e "$release" ]] || { printf 'Release already exists.\n' >&2; exit 1; }
install -d -m700 "$dest/releases"
staging=$(mktemp -d "$dest/releases/.staging.XXXXXX")
printf 'stage=copy-reviewed-source\n' >&3
for file in claude_primary.py chatgpt_pm.py README.md claude-primary.env.example claude-primary.conf.example claude-sources.example.json; do
  install -m400 "$src/$file" "$staging/$file"
done
(cd "$staging" && sha256sum -- * > MANIFEST && chmod 400 MANIFEST)
chmod 500 "$staging"
mv -T -- "$staging" "$release"
staging=''
printf 'stage=release-staged\n' >&3
printf '%s\n' "$release"

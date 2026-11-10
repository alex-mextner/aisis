#!/usr/bin/env bash
# Installs the systemd USER units that this repository versions (services/*/systemd) and turns them on.
# Idempotent: a second run changes no file and restarts nothing (it does reload systemd, which is harmless).
#
#   install-systemd-units.sh              copy every unit and drop-in into ~/.config/systemd/user, daemon-reload,
#                                         enable each unit that has an [Install] section, start the ones not running
#   install-systemd-units.sh --no-start   install and enable, but never start a unit (--check still reports a stopped one)
#   install-systemd-units.sh --plan       read-only: what a run would do (exit 0)
#   install-systemd-units.sh --check      read-only: exit 0 = installed, enabled and running, 3 = drift (one line each)
#
# Exit codes: 0 ok, 3 drift (--check; an enabled unit that is not running counts), 10 needs a manual step (no user systemd session, or a unit that did not start:
# every unit is still enabled and the others still started), anything else is a failure.
#
# What it does and does not do:
#   - Files are COPIED (mode 0644), not linked: a unit keeps working when the checkout moves, and a dangling symlink
#     into a vanished checkout cannot take the voice stack down. An existing symlink at a destination is replaced by
#     the copy. The repository stays the only place a unit is edited: `--check` reports every file that differs.
#   - A unit that is already running is never restarted, even when its file changed; the line
#     "restart to apply: <unit>" names it, and the owner restarts it when a gap in speech is acceptable.
#   - Units without [Install] (for example a oneshot started by its timer) are installed, not enabled.
#   - Runtime state (venvs, env files with secrets under ~/.config/aisis, models) is NOT created here; units that
#     need it fail to start on a machine that lacks it and are retried by systemd's Restart= policy.
#   - HOME / XDG_CONFIG_HOME decide the destination, so --plan and --check can be run against a scratch directory.
set -euo pipefail

mode=install
start=1
for arg in "$@"; do
  case "$arg" in
    --check) mode=check ;;
    --plan) mode=plan ;;
    --no-start) start=0 ;;
    -h|--help) sed -n '2,23p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "usage: $0 [--plan|--check|--no-start]" >&2; exit 2 ;;
  esac
done

repo=$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)
dest="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"

# (source directory, relative file) pairs: top-level unit files and the drop-ins of <unit>.d/.
files=()   # "<source dir>|<relative path>"
units=()   # unit names (top-level files only)
for src in "$repo"/services/*/systemd; do
  [ -d "$src" ] || continue
  while IFS= read -r rel; do
    files+=("$src|$rel")
    case "$rel" in */*) ;; *) units+=("$rel") ;; esac
  done < <(find "$src" -type f -printf '%P\n' | sort)
done
if [ "${#files[@]}" -eq 0 ]; then echo "no units under $repo/services/*/systemd" >&2; exit 1; fi

# WantedBy= targets of a unit file ([Install] section only); empty when the unit has no [Install].
wanted_by() {
  awk '/^\[/ { in_install = ($0 == "[Install]"); next } in_install && /^WantedBy=/ { sub(/^WantedBy=/, ""); print }' "$1" | tr ' ' '\n' | sed '/^$/d'
}
src_of() { local pair; for pair in "${files[@]}"; do [ "${pair#*|}" = "$1" ] && { echo "${pair%%|*}/$1"; return; }; done; }

# Drift of a copied file: prints one word (missing | symlink | differs | mode) or nothing when it is in place.
# A drop-in whose directory is itself a link into a checkout counts as a symlink: the copy replaces the whole link.
file_state() { # file_state SRC DEST
  local parent; parent=$(dirname "$2")
  if [ -L "$2" ] || { [ "$parent" != "$dest" ] && [ -L "$parent" ]; }; then echo symlink
  elif [ ! -e "$2" ]; then echo missing
  elif ! cmp -s "$1" "$2"; then echo differs
  elif [ "$(stat -c %a "$2")" != 644 ]; then echo mode
  fi
}
link_enabled() { [ -L "$1" ] && [ "$(readlink -f "$1")" != /dev/null ]; }   # a link to /dev/null is a mask, not an enable
# Enabled = every WantedBy target holds a symlink for the unit (what `systemctl --user enable` writes).
unit_enabled() { # unit_enabled UNIT SRC
  local target any=0
  while IFS= read -r target; do
    any=1
    link_enabled "$dest/$target.wants/$1" || link_enabled "$dest/$target.requires/$1" || return 1
  done < <(wanted_by "$2")
  [ "$any" -eq 1 ]
}

# Loaded from the copy installed under $dest, wanted running, and not active: a start that failed once must be retried.
# Not verifiable (no user session, or systemd loaded the unit from elsewhere: a scratch HOME is not the real one) is
# neither drift nor a clean verdict: `unverified` makes the final report say so.
unverified=0
unit_inactive() {
  if ! systemctl --user show-environment >/dev/null 2>&1 \
     || [ "$(systemctl --user show -p FragmentPath --value "$1" 2>/dev/null)" != "$dest/$1" ]; then
    unverified=1; return 1
  fi
  [ "$(systemctl --user is-active "$1" 2>/dev/null || true)" != active ]
}

drift=()
for pair in "${files[@]}"; do
  rel=${pair#*|}; state=$(file_state "${pair%%|*}/$rel" "$dest/$rel")
  [ -z "$state" ] || drift+=("$state: $dest/$rel")
done
for unit in "${units[@]}"; do
  [ -n "$(wanted_by "$(src_of "$unit")")" ] || continue
  unit_enabled "$unit" "$(src_of "$unit")" || drift+=("not enabled: $unit")
  if unit_inactive "$unit"; then drift+=("not active: $unit"); fi
done

if [ "$mode" != install ]; then
  if [ "${#drift[@]}" -eq 0 ]; then
    if [ "$unverified" -eq 0 ]; then
      echo "ok: ${#units[@]} units and their drop-ins match $repo, all enabled and running"
    else
      echo "ok: ${#units[@]} units and their drop-ins match $repo, all enabled (not verified running: no user systemd session, or systemd loads them from elsewhere)"
    fi
    exit 0
  fi
  printf '%s\n' "${drift[@]}"
  [ "$mode" = plan ] && exit 0
  exit 3
fi

if ! systemctl --user show-environment >/dev/null 2>&1; then
  echo "needs manual: no user systemd session reachable (run from a login session with systemd --user, or enable linger)" >&2
  exit 10
fi

# A drop-in directory that is a link into a checkout (possibly a dead one) is removed before anything is written, so
# no copy is ever written through the link; every file of the repository is then installed fresh (and a running unit
# is named for restart: the link may have carried drop-ins this repository does not).
links=()
for pair in "${files[@]}"; do
  rel=${pair#*|}
  case "$rel" in */*) ;; *) continue ;; esac
  linked=$dest/${rel%/*}
  [ -L "$linked" ] || continue
  case " ${links[*]-} " in *" $linked "*) ;; *) links+=("$linked") ;; esac
done
for linked in "${links[@]-}"; do
  [ -n "$linked" ] || continue
  rm -f "$linked"
  echo "replaced link: $linked"
done

for pair in "${files[@]}"; do
  src=${pair%%|*}/${pair#*|}; rel=${pair#*|}; target=$dest/$rel
  state=$(file_state "$src" "$target")
  [ -z "$state" ] && continue
  if [ -L "$target" ]; then rm -f "$target"; fi
  mkdir -p "$(dirname "$target")"
  install -m 0644 "$src" "$target"
  echo "installed: $target"
  [ "$state" = mode ] && continue   # same content, only the permissions were repaired: nothing to restart
  unit=${rel%%/*}; unit=${unit%.d}
  # Said at once, not at the end: a later copy or reload failure must not swallow it, the retry finds the file in place.
  if [ "$(systemctl --user is-active "$unit" 2>/dev/null || true)" = active ]; then echo "restart to apply: $unit"; fi
done
# Every run reloads: a reload that failed on an earlier run left matching files behind and must still be retried.
systemctl --user daemon-reload

# Enable everything first, so a unit that fails to start below still comes back at the next login.
for unit in "${units[@]}"; do
  [ -n "$(wanted_by "$(src_of "$unit")")" ] || continue
  if ! unit_enabled "$unit" "$(src_of "$unit")"; then systemctl --user enable --force "$unit" >/dev/null; echo "enabled: $unit"; fi
done

failed=()
if [ "$start" -eq 1 ]; then
  for unit in "${units[@]}"; do
    [ -n "$(wanted_by "$(src_of "$unit")")" ] || continue
    [ "$(systemctl --user is-active "$unit" 2>/dev/null || true)" = active ] && continue
    # `start` succeeds for a unit that systemd skips (a failed Condition...=), so the state after it decides
    if systemctl --user start "$unit" && [ "$(systemctl --user is-active "$unit" 2>/dev/null || true)" = active ]; then
      echo "started: $unit"
    else
      failed+=("$unit")
    fi
  done
fi

if [ "${#failed[@]}" -gt 0 ]; then
  echo "needs manual: ${#failed[@]} unit(s) did not come up: ${failed[*]} (check runtime state under ~/.config/aisis and ~/.local/share/aisis, then journalctl --user -u <unit>)" >&2
  exit 10
fi
exit 0

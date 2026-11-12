#!/usr/bin/env bash
# Make aisis-failover.service skip components that run on the always-on LAN front instead of here.
#
#     install-failover-exclude.sh [--component NAME ...]    default components: tls-voice dns-relay
#     install-failover-exclude.sh --dry-run ...              print the drop-in, change nothing
#     install-failover-exclude.sh --uninstall                remove the drop-in and the launcher, restart the watchdog
#
# Installs the launcher to $AISIS_CONFIG_DIR/failover-exclude.py (default ~/.config/aisis) and a user-systemd drop-in that
# keeps every original argument of the unit's ExecStart but starts the launcher in place of failover.py. The drop-in also
# exports AISIS_BLUEGREEN_DIR pointing at the directory that holds the deployed failover.py (parsed from the unit's own
# ExecStart; the launcher's default ~/.local/share/aisis-yandex/bluegreen is only a fallback for other deployments). All
# other components stay watched; the journal shows the `failover-exclude: watching ...` line after the restart.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
AISIS_CONFIG_DIR=${AISIS_CONFIG_DIR:-$HOME/.config/aisis}
UNIT=${AISIS_FAILOVER_UNIT:-aisis-failover.service}
DROPIN_DIR=${AISIS_FAILOVER_DROPIN_DIR:-$HOME/.config/systemd/user/$UNIT.d}
DROPIN=$DROPIN_DIR/front-exclude.conf
LAUNCHER=$AISIS_CONFIG_DIR/failover-exclude.py
SYSTEMCTL=${SYSTEMCTL:-systemctl}

components=()
dry=0
uninstall=0
while [ $# -gt 0 ]; do
  case "$1" in
    --component) components+=("${2:?--component needs a NAME}"); shift 2 ;;
    --dry-run) dry=1; shift ;;
    --uninstall) uninstall=1; shift ;;
    *) echo "usage: $0 [--component NAME ...] [--dry-run] [--uninstall]" >&2; exit 2 ;;
  esac
done
[ "${#components[@]}" -gt 0 ] || components=(tls-voice dns-relay)

say() { echo "==> $*"; }
die() { echo "FAIL: $*" >&2; exit 1; }

if [ "$uninstall" -eq 1 ]; then
  if [ "$dry" -eq 1 ]; then
    say "DRY RUN: would remove $DROPIN and $LAUNCHER and restart $UNIT"
    exit 0
  fi
  rm -f "$DROPIN" "$LAUNCHER"
  rmdir "$DROPIN_DIR" 2>/dev/null || true
  $SYSTEMCTL --user daemon-reload
  $SYSTEMCTL --user restart "$UNIT"
  say "removed; $UNIT runs its original command again"
  exit 0
fi

# Original command: the drop-in replaces ExecStart, so it must be rebuilt from the unit's own line (without our drop-in).
# Only a `# /path` line starts a file in `systemctl cat` output: the drop-in's own `# ...` comment lines must not
# reset the parser (they used to, which made every re-run and even --dry-run fail after the first install).
original=$($SYSTEMCTL --user cat "$UNIT" 2>/dev/null | awk -v d="$DROPIN" '/^# \//{file=$2} file!=d && /^ExecStart=./{line=$0} END{sub(/^ExecStart=/,"",line); print line}') \
  || die "cannot read ExecStart of $UNIT (is the unit installed?)"
case "$original" in *failover.py*) ;; *) die "ExecStart of $UNIT does not run failover.py: $original" ;; esac
command=$(printf '%s\n' "$original" | sed -E "s#[^ ]*failover\\.py#$LAUNCHER#")
# failover.py lives wherever the unit's ExecStart says, rarely in the launcher's default directory — export the real
# directory to the validation call and the drop-in. Specifiers such as %h stay as-is for systemd to expand in the unit;
# the local validation shell expands %h itself.
bluegreen_dir=$(printf '%s\n' "$original" | grep -oE '[^ ]*failover\.py' | head -n1)
bluegreen_dir=${bluegreen_dir%/failover.py}
[ -n "$bluegreen_dir" ] || die "cannot locate the failover.py directory in ExecStart of $UNIT: $original"
for name in "${components[@]}"; do
  [[ $name =~ ^[a-z0-9-]+$ ]] || die "component name '$name' is not valid"
  command="$command --exclude-component $name"
done

# Bash pattern replacement instead of sed: the command line may contain `&`, `\` or `#`, which sed would reinterpret.
rendered=$(<"$here/aisis-failover-exclude.conf.template")
# quoted replacements: bash >= 5.2 patsub_replacement would otherwise reinterpret & and \ in the command line
rendered=${rendered//@ORIGINAL_COMMAND@/"$command"}
rendered=${rendered//@BLUEGREEN_DIR@/"$bluegreen_dir"}
if [ "$dry" -eq 1 ]; then
  say "DRY RUN: would install $LAUNCHER and write $DROPIN:"
  printf '%s\n' "$rendered" | sed 's/^/    /'
  exit 0
fi

mkdir -p "$AISIS_CONFIG_DIR" "$DROPIN_DIR"
chmod 700 "$AISIS_CONFIG_DIR"
install -m 700 "$here/failover-exclude.py" "$LAUNCHER"
# Validate every name against the deployed watchdog BEFORE the unit is touched.
listing=$(AISIS_BLUEGREEN_DIR=${bluegreen_dir//%h/$HOME} python3 -B "$LAUNCHER" --print-components "${components[@]/#/--exclude-component=}") \
  || die "launcher rejected the component names or could not import the watchdog from $bluegreen_dir"
printf '%s\n' "$rendered" > "$DROPIN.new"
mv "$DROPIN.new" "$DROPIN"
$SYSTEMCTL --user daemon-reload
$SYSTEMCTL --user restart "$UNIT"
# Type=simple makes `restart` return before the process proves it can run: give it a second, then require it active.
sleep 1
$SYSTEMCTL --user is-active --quiet "$UNIT" || die "$UNIT is not active after the restart (see: journalctl --user -u $UNIT)"
# drop-ins apply in filename order: a later one (e.g. override.conf from `systemctl --user edit`) could override our
# ExecStart while is-active still passes — prove the launcher is really what the unit runs now
$SYSTEMCTL --user show -p ExecStart --value "$UNIT" | grep -qF "$LAUNCHER" \
  || die "another drop-in overrides ours: $UNIT does not run $LAUNCHER; remove that drop-in (systemctl --user cat $UNIT lists them)"
say "restarted $UNIT (active, launcher confirmed in ExecStart)"
echo "    $listing"
echo "    undo: $0 --uninstall"

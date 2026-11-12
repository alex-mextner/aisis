#!/usr/bin/env bash
# Test the failover exclusion launcher and its installer against a stub watchdog module and a fake systemctl (no
# live service is touched).   bash test-failover-exclude.sh
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
T=$(mktemp -d)
trap 'rm -rf "$T"' EXIT
fail=0
check() { if [ "$2" = "$3" ]; then echo "PASS $1"; else echo "FAIL $1: got [$2] want [$3]"; fail=1; fi; }

# stub of the deployed watchdog: a COMPONENTS tuple with .name, and a main() that reports what it would watch
mkdir -p "$T/bluegreen"
cat > "$T/bluegreen/failover.py" <<'PY'
import sys
from collections import namedtuple
C = namedtuple("C", "name")
COMPONENTS = tuple(C(n) for n in ("adapter", "stt", "tls-voice", "dns-relay", "kit"))
def main():
    print("MAIN argv=" + " ".join(sys.argv[1:]) + " watched=" + ",".join(c.name for c in COMPONENTS))
    return 0
PY
export AISIS_BLUEGREEN_DIR=$T/bluegreen
L=$here/failover-exclude.py
run() { python3 -B "$L" "$@" 2>&1 || true; }

check "default lists every component" "$(run --print-components)" "failover-exclude: watching adapter, stt, tls-voice, dns-relay, kit; excluded none"
check "excluding two removes exactly those" "$(run --print-components --exclude-component tls-voice --exclude-component dns-relay)" "failover-exclude: watching adapter, stt, kit; excluded dns-relay, tls-voice"
check "=NAME form" "$(run --print-components --exclude-component=kit | tail -n1)" "failover-exclude: watching adapter, stt, tls-voice, dns-relay; excluded kit"
check "typo is rejected, nothing watched-less" "$(run --print-components --exclude-component tls-vioce | head -n1)" "failover-exclude: unknown component(s) tls-vioce; known: adapter, stt, tls-voice, dns-relay, kit"
python3 -B "$L" --print-components --exclude-component nope >/dev/null 2>&1 && rc=0 || rc=$?
check "unknown name exits 2" "$rc" 2
python3 -B "$L" --exclude-component >/dev/null 2>&1 && rc=0 || rc=$?
check "missing NAME exits 2" "$rc" 2
check "other arguments reach main(), filter applied" "$(run --env prod --interval 2 --exclude-component stt | tail -n1)" "MAIN argv=--env prod --interval 2 watched=adapter,tls-voice,dns-relay,kit"

# installer with a fake systemctl: records calls, serves a unit whose ExecStart deploys the watchdog OUTSIDE the
# launcher's default ~/.local/share/aisis-yandex/bluegreen (here: $T/bluegreen) — the installer must take the
# directory from the unit itself, and the launcher runs without any ambient AISIS_BLUEGREEN_DIR
mkdir -p "$T/bin" "$T/home/dropin"
cat > "$T/bin/systemctl" <<'SH'
echo "systemctl $*" >> "$FAKE_LOG"
if [ "$2" = show ]; then
  # like the real `systemctl --user show -p ExecStart --value`: the effective command, drop-ins applied
  [ -f "$FAKE_DROPIN" ] && printf 'ExecStart={ path=/usr/bin/python3 ; argv[]=/usr/bin/python3 %s --env prod --exclude-component tls-voice --exclude-component dns-relay }\n' "$FAKE_LAUNCHER"
  exit 0
fi
if [ "$2" = cat ]; then
  echo "# $FAKE_UNIT"
  cat "$FAKE_UNIT"
  # serve the drop-in VERBATIM (comments included), like the real `systemctl --user cat`: the installer must not
  # confuse the drop-in's own `# ...` comment lines with file headers
  [ -f "$FAKE_DROPIN" ] && { echo "# $FAKE_DROPIN"; cat "$FAKE_DROPIN"; }
fi
exit 0
SH
chmod +x "$T/bin/systemctl"
export FAKE_LOG=$T/calls.log FAKE_DROPIN=$T/home/dropin/front-exclude.conf FAKE_UNIT=$T/unit FAKE_LAUNCHER=$T/cfg/failover-exclude.py
# the unit itself carries comment lines, like real deployed units do
printf '# /fake/aisis-failover.service\n# a deployed unit with its own comments\n[Service]\nExecStart=/usr/bin/python3 %s/bluegreen/failover.py --env prod\n' "$T" > "$FAKE_UNIT"
inst() { env -u AISIS_BLUEGREEN_DIR SYSTEMCTL=$T/bin/systemctl AISIS_CONFIG_DIR=$T/cfg AISIS_FAILOVER_DROPIN_DIR=$T/home/dropin bash "$here/install-failover-exclude.sh" "$@"; }

dry=$(inst --dry-run)
check "dry run prints the drop-in with every original argument" "$(printf '%s\n' "$dry" | grep -c "ExecStart=/usr/bin/python3 $T/cfg/failover-exclude.py --env prod --exclude-component tls-voice --exclude-component dns-relay")" 1
check "dry run changes nothing" "$([ ! -e "$T/cfg" ] && [ ! -e "$FAKE_DROPIN" ] && ! grep -q 'daemon-reload\|restart' "$FAKE_LOG" && echo clean)" clean

listing=$(inst)
check "validation imports the watchdog via the dir parsed from ExecStart" "$(printf '%s\n' "$listing" | grep -c 'failover-exclude: watching adapter, stt, kit')" 1
check "launcher installed executable owner-only" "$(stat -c %a "$T/cfg/failover-exclude.py")" 700
check "drop-in written" "$(grep -c '^ExecStart=$' "$FAKE_DROPIN")" 1
check "drop-in exports AISIS_BLUEGREEN_DIR parsed from ExecStart" "$(grep -c "^Environment=AISIS_BLUEGREEN_DIR=$T/bluegreen\$" "$FAKE_DROPIN")" 1
check "daemon-reload then restart, then the ExecStart proof" "$(grep -c 'daemon-reload\|restart\|show -p ExecStart' "$FAKE_LOG")" 3
check "re-install keeps a single original command (idempotent, drop-in comments included in systemctl cat)" "$(inst >/dev/null && grep -c "^ExecStart=/usr" "$FAKE_DROPIN")" 1
check "re-install does not stack the launcher" "$(grep -c 'failover-exclude.py.*failover-exclude.py' "$FAKE_DROPIN")" 0
check "dry run still works after an install (drop-in with comments present)" "$(inst --dry-run >/dev/null && echo ok)" ok
check "bad component name is refused before any change" "$(inst --component nope >/dev/null 2>&1 && echo accepted || echo refused)" refused

# live shape: the unit addresses the watchdog through the %h specifier — the drop-in keeps it for systemd to expand,
# the validation call expands it against a fake home that really holds the stub watchdog
mkdir -p "$T/fakehome/bluegreen"
cp "$T/bluegreen/failover.py" "$T/fakehome/bluegreen/"
printf '# /fake/aisis-failover.service\n[Service]\nExecStart=/usr/bin/python3 %%h/bluegreen/failover.py --env prod\n' > "$FAKE_UNIT"
env -u AISIS_BLUEGREEN_DIR HOME=$T/fakehome SYSTEMCTL=$T/bin/systemctl AISIS_CONFIG_DIR=$T/cfg AISIS_FAILOVER_DROPIN_DIR=$T/home/dropin \
  bash "$here/install-failover-exclude.sh" >/dev/null 2>&1 && rc=0 || rc=$?
check "%h unit: validation imports from the expanded home" "$rc" 0
check "%h unit: drop-in keeps %h for systemd to expand" "$(grep -c '^Environment=AISIS_BLUEGREEN_DIR=%h/bluegreen$' "$FAKE_DROPIN")" 1

inst --uninstall >/dev/null
check "uninstall removes drop-in and launcher" "$([ ! -e "$FAKE_DROPIN" ] && [ ! -e "$T/cfg/failover-exclude.py" ] && echo gone)" gone
exit "$fail"

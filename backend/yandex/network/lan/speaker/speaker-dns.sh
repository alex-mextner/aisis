#!/usr/bin/env bash
# Point the speaker's resolver at the LAN front, and undo it. Talks to the speaker over adb; touches only the pinned
# resolver file and asks dhcpcd to regenerate /etc/resolv.conf. DHCP settings, the speaker's address and its firewall
# are not changed.
#
#     speaker-dns.sh plan      read-only checks, then print exactly what apply would do
#     speaker-dns.sh apply     back up /data/resolv.conf.head, write one `nameserver <resolver>` line, regenerate,
#                              verify; reverts by itself if the verification fails
#     speaker-dns.sh undo      restore the newest backup (or: undo --backup /data/resolv.conf.head.pre-front-<ts>)
#
# Values come from the private lan.env (see config/lan.env.example): AISIS_LAN_SPEAKER_ADB, AISIS_LAN_RESOLVER,
# AISIS_LAN_FRONT_ADDR, AISIS_LAN_SPEAKER_ADDR. The mechanism (resolv.conf.head -> /data, dhcpcd hook) is re-verified on
# the device before anything is written.
set -euo pipefail
# shellcheck source=../bin/lan-lib.sh
. "$(dirname "$0")/../bin/lan-lib.sh"

MODE=${1:-}
shift || true
BACKUP_ARG=""
SKIP_OVERRIDE_CHECK=0
while [ $# -gt 0 ]; do
  case "$1" in
    --backup) BACKUP_ARG=${2:?--backup needs a device path}; shift 2 ;;
    --no-override-check) SKIP_OVERRIDE_CHECK=1; shift ;;
    *) echo "usage: $0 plan|apply|undo [--backup DEVICE_PATH] [--no-override-check]" >&2; exit 2 ;;
  esac
done
case "$MODE" in plan|apply|undo) ;; *) echo "usage: $0 plan|apply|undo [--backup DEVICE_PATH]" >&2; exit 2 ;; esac

lan_load AISIS_LAN_SPEAKER_ADB AISIS_LAN_RESOLVER AISIS_LAN_FRONT_ADDR AISIS_LAN_SPEAKER_ADDR
ADB=$AISIS_LAN_SPEAKER_ADB
HEAD_FILE=/data/resolv.conf.head
CANARY_NAME=${AISIS_LAN_CANARY_NAME:-uniproxy.alice.yandex.net}

# adb shell does not propagate exit codes on this device: print markers and read output instead.
dev() { adb -s "$ADB" shell "$@" | tr -d '\r'; }

preflight() {
  lan_say "read-only checks on the speaker ($ADB)"
  adb connect "$ADB" >/dev/null 2>&1 || true
  [ "$(adb -s "$ADB" get-state 2>/dev/null || true)" = device ] || lan_die "adb device $ADB is not connected (adb connect $ADB)"
  [ "$(dev 'id -u')" = 0 ] || lan_die "adb shell is not root on the speaker"
  lan_ok "adb, root"
  local link
  link=$(dev 'readlink /etc/resolv.conf.head')
  [ "$link" = "$HEAD_FILE" ] || lan_die "/etc/resolv.conf.head resolves to '$link', not $HEAD_FILE: the pinned-resolver mechanism changed, stop and re-check"
  lan_ok "/etc/resolv.conf.head -> $HEAD_FILE"
  [ -n "$(dev 'command -v dhcpcd')" ] && [ -n "$(dev 'ps | grep "[d]hcpcd: .manager"')" ] || lan_die "dhcpcd is not running on the speaker"
  lan_ok "dhcpcd running"
  local iface server
  iface=$(dev "ifconfig | grep -B1 'inet addr:$AISIS_LAN_SPEAKER_ADDR ' | head -n1 | cut -d' ' -f1")
  [ -n "$iface" ] || lan_die "no interface on the speaker carries $AISIS_LAN_SPEAKER_ADDR"
  server=$(dev "dhcpcd -U $iface 2>/dev/null | sed -n 's/^dhcp_server_identifier=//p'")
  [ "$server" = "$AISIS_LAN_FRONT_ADDR" ] || lan_die "the speaker's lease comes from '$server', not the front $AISIS_LAN_FRONT_ADDR: move DHCP first"
  lan_ok "lease server is the front"
  if [ "$SKIP_OVERRIDE_CHECK" -eq 0 ]; then
    local answer
    answer=$(dev "nslookup $CANARY_NAME $AISIS_LAN_RESOLVER 2>&1 | grep '^Address' | tail -n +2 | sed 's/^Address[^:]*: *//' | tr '\n' ' '")
    case " $answer" in
      *" $AISIS_LAN_FRONT_ADDR "*) lan_ok "resolver $AISIS_LAN_RESOLVER answers the speaker for $CANARY_NAME with the front address" ;;
      *) lan_die "resolver $AISIS_LAN_RESOLVER answered '$answer' for $CANARY_NAME (expected the front $AISIS_LAN_FRONT_ADDR): the relay override is off or unreachable for this speaker" ;;
    esac
  fi
  echo "    current /etc/resolv.conf:"
  dev 'cat /etc/resolv.conf' | sed 's/^/      /'
  echo "    current $HEAD_FILE:"
  dev "cat $HEAD_FILE" | sed 's/^/      /'
}

regenerate() {
  # dhcpcd -n returns before it rewrites /etc/resolv.conf, and a leftover 'nameserver <resolver>' line from an
  # older head file would end the wait at once on the stale content: wait for the file to actually change.
  local _ conf before first
  before=$(dev 'cat /etc/resolv.conf')
  dev 'dhcpcd -n' >/dev/null || true
  for _ in $(seq 1 "${AISIS_SPEAKER_WAIT_S:-20}"); do
    conf=$(dev 'cat /etc/resolv.conf')
    if [ "$conf" != "$before" ] && printf '%s\n' "$conf" | grep -q "^nameserver $1\$"; then return 0; fi
    sleep 1
  done
  # dhcpcd may legitimately rebuild identical content when nothing changed: accept it when the resolver is in place
  [ -n "$(dev "grep -c '^nameserver $1\$' /etc/resolv.conf | grep -v '^0\$'")" ] || return 1
  first=$(dev "sed -n 's/^nameserver //p' /etc/resolv.conf" | head -n1)
  [ "$first" = "$1" ]
}

verify() { # resolver -> prints resolv.conf; fails unless the resolver is first and nothing foreign is listed
  local resolver=$1 conf foreign first
  conf=$(dev 'cat /etc/resolv.conf')
  echo "    /etc/resolv.conf now:"
  printf '%s\n' "$conf" | sed 's/^/      /'
  first=$(printf '%s\n' "$conf" | sed -n 's/^nameserver //p' | head -n1)
  foreign=$(printf '%s\n' "$conf" | sed -n 's/^nameserver //p' | grep -vxF -e "$resolver" -e 127.0.0.1 || true)
  [ "$first" = "$resolver" ] && [ -z "$foreign" ] || return 1
  [ -n "$(dev "ifconfig | grep -F 'inet addr:$AISIS_LAN_SPEAKER_ADDR '")" ] || return 1
}

case "$MODE" in
  plan)
    preflight
    lan_say "PLAN (nothing changed)"
    echo "    cp -p $HEAD_FILE $HEAD_FILE.pre-front-<timestamp>"
    printf "    printf 'nameserver %s\\\\n' > %s.new && chmod 644 %s.new && mv %s.new %s\\n" "$AISIS_LAN_RESOLVER" "$HEAD_FILE" "$HEAD_FILE" "$HEAD_FILE" "$HEAD_FILE"
    echo "    dhcpcd -n      # rebind: dhcpcd rebuilds /etc/resolv.conf (head file + 127.0.0.1) through its hook"
    echo "    verify: first nameserver = $AISIS_LAN_RESOLVER, no other server except 127.0.0.1, interface address unchanged"
    echo "    on any verification failure: restore the backup, dhcpcd -n again"
    ;;
  apply)
    preflight
    if [ "$(dev "cat $HEAD_FILE")" = "nameserver $AISIS_LAN_RESOLVER" ]; then
      lan_ok "$HEAD_FILE already is 'nameserver $AISIS_LAN_RESOLVER'; nothing to do (no second backup, so undo keeps the original)"
      verify "$AISIS_LAN_RESOLVER" || lan_die "the head file is set but /etc/resolv.conf does not match; run: $0 undo, then apply again"
      exit 0
    fi
    ts=$(dev 'date +%Y%m%d-%H%M%S')
    backup=$HEAD_FILE.pre-front-$ts
    lan_say "backup $backup"
    dev "cp -p $HEAD_FILE $backup && echo backed-up" | grep -qx backed-up || lan_die "backup failed; nothing changed"
    lan_say "write $HEAD_FILE: nameserver $AISIS_LAN_RESOLVER"
    dev "printf 'nameserver $AISIS_LAN_RESOLVER\\n' > $HEAD_FILE.new && chmod 644 $HEAD_FILE.new && mv $HEAD_FILE.new $HEAD_FILE"
    lan_say "regenerate the resolver (dhcpcd -n)"
    if regenerate "$AISIS_LAN_RESOLVER" && verify "$AISIS_LAN_RESOLVER"; then
      lan_ok "speaker resolver is now $AISIS_LAN_RESOLVER; undo: $0 undo --backup $backup"
    else
      echo "FAIL: verification failed, restoring $backup" >&2
      dev "cp -p $backup $HEAD_FILE" >/dev/null
      dev 'dhcpcd -n' >/dev/null || true
      sleep 3
      dev 'cat /etc/resolv.conf' | sed 's/^/      /' >&2
      exit 1
    fi
    ;;
  undo)
    adb connect "$ADB" >/dev/null 2>&1 || true
    backup=$BACKUP_ARG
    [ -n "$backup" ] || backup=$(dev "ls -1 $HEAD_FILE.pre-front-* 2>/dev/null | sort | tail -n1")
    [ -n "$backup" ] || lan_die "no $HEAD_FILE.pre-front-* backup found on the speaker"
    [[ $backup =~ ^/data/resolv\.conf\.head\.pre-front-[0-9]{8}-[0-9]{6}$ ]] || lan_die "backup must look like /data/resolv.conf.head.pre-front-YYYYMMDD-HHMMSS"
    [ "$(dev "[ -f $backup ] && echo yes")" = yes ] || lan_die "$backup does not exist on the speaker"
    old=$(dev "sed -n 's/^nameserver //p' $backup | head -n1")
    if [ -n "$old" ]; then
      lan_say "restore $backup (resolver $old)"
    else
      # the original head file may legitimately have had no nameserver line (empty or comments only): restoring it
      # still removes the front from the resolver, there is just no line to wait for
      lan_say "restore $backup (no nameserver line in the backup)"
    fi
    dev "cp -p $backup $HEAD_FILE.new && mv $HEAD_FILE.new $HEAD_FILE"
    dev 'dhcpcd -n' >/dev/null || true
    if [ -n "$old" ]; then
      regenerate "$old" || lan_die "resolver did not return to $old in time; check /etc/resolv.conf by hand"
    fi
    dev 'cat /etc/resolv.conf' | sed 's/^/      /'
    [ -n "$old" ] && lan_ok "restored $old from $backup" || lan_ok "restored $backup (the front is gone from the resolver)"
    ;;
esac

#!/usr/bin/env bash
# Health and path proof for the always-on LAN front. Read-only; run from the workstation.
#
#     verify.sh [--since 10m]        window for the log proofs (s|m|h, default 10m)
#
# Checks: front reachable, relay/TLS containers up, relay override state, the TLS leaf fingerprint, and the PROOF that
# the speaker uses the front: its lines in the front's TLS log and none in the old (workstation) copy's TLS log.
# Values come from the private lan.env. Exit 1 on any FAIL.
set -euo pipefail
# shellcheck source=lan-lib.sh
. "$(dirname "$0")/lan-lib.sh"

since=10m
while [ $# -gt 0 ]; do
  case "$1" in
    --since) since=${2:?--since needs a duration like 10m}; shift 2 ;;
    *) echo "usage: $0 [--since 10m]" >&2; exit 2 ;;
  esac
done
[[ $since =~ ^([0-9]+)([smh])$ ]] || lan_die "--since must look like 90s, 10m or 2h"

lan_load AISIS_LAN_FRONT_ADDR AISIS_LAN_FRONT_SSH AISIS_LAN_SPEAKER_ADDR AISIS_LAN_PROJECT AISIS_LAN_FRONT_DIR
relay=$AISIS_LAN_PROJECT-dns-relay
tls=$AISIS_LAN_PROJECT-tls-voice
local_tls=${AISIS_LAN_LOCAL_TLS_CONTAINER:-aisis-yandex-network-tls-voice-1}
sni=${AISIS_LAN_CANARY_NAME:-uniproxy.alice.yandex.net}
rc=0
bad() { echo "FAIL: $*" >&2; rc=1; }

lan_say "front host"
lan_front true 2>/dev/null || { bad "cannot ssh to the front host"; exit 1; }
lan_ok "ssh"
for c in "$relay" "$tls"; do
  if [ "$(lan_front "docker inspect -f '{{.State.Running}}' $c 2>/dev/null" || true)" = true ]; then lan_ok "$c running"; else bad "$c is not running"; fi
done
for c in "$AISIS_LAN_PROJECT-dhcp" "$AISIS_LAN_PROJECT-backend-watcher"; do
  state=$(lan_front "docker inspect -f '{{.State.Status}}' $c 2>/dev/null" | tr -d '\n' || true)
  echo "    info: $c ${state:-absent}"
done

lan_say "relay override"
ready=$(lan_front "docker logs $relay 2>&1 | grep dns_ready | tail -n1" || true)
echo "    ${ready:-no dns_ready line (rotated out of the log; see dns_counts)}"
counts=$(lan_front "docker logs --tail 200 $relay 2>&1 | grep dns_counts | tail -n1" || true)
echo "    ${counts:-no dns_counts line}"
case "$ready" in *'"override_enabled": false'*) bad "the relay starts with the override OFF: the speaker would get no replacement addresses" ;; esac

lan_say "TLS front"
fp=$(echo | openssl s_client -connect "$AISIS_LAN_FRONT_ADDR:443" -servername "$sni" 2>/dev/null | openssl x509 -noout -fingerprint -sha256 2>/dev/null || true)
if [ -n "$fp" ]; then lan_ok "leaf for SNI $sni: ${fp#*=}"; else bad "no TLS answer from $AISIS_LAN_FRONT_ADDR:443 for SNI $sni"; fi

lan_say "speaker path proof (last $since)"
front_all=$(lan_front "docker logs --since $since $tls 2>&1 | grep '^$AISIS_LAN_SPEAKER_ADDR '" || true)
front_lines=$(printf '%s\n' "$front_all" | tail -n 8)
if [ -n "$front_lines" ]; then
  printf '%s\n' "$front_lines" | sed 's/^/    front TLS: /'
  # the whole window, not just the tail: nginx logs the 101 line only when the WebSocket closes, so it can be older than 8 lines
  if printf '%s\n' "$front_all" | awk -v s="$sni" '$2==s && $4==101 {f=1} END {exit !f}'; then
    lan_ok "voice websocket from the speaker reached the backend via the front"
  else
    bad "speaker lines but no 101 (WebSocket upgrade) for SNI $sni in the front TLS log: the voice path is NOT proven"
  fi
else
  bad "no speaker lines in the front's TLS log: the voice path via the front is NOT verified (use the speaker, then re-run)"
fi
if docker inspect "$local_tls" >/dev/null 2>&1; then
  old=$(docker logs --since "$since" "$local_tls" 2>&1 | grep -c "^$AISIS_LAN_SPEAKER_ADDR " || true)
  echo "    old copy ($local_tls): $old speaker line(s) in the same window"
  [ "$old" = 0 ] || bad "the old copy still received $old speaker line(s): the speaker has not left it"
fi
exit "$rc"

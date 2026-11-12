#!/usr/bin/env bash
# Start a capture on this workstation and on the LAN front together, fetch the front's pcap, print both summaries.
# NOT run by default.     capture-both.sh [--seconds N]        (N default 120, max 300)
# Do the action under investigation (e.g. power-cycle the speaker) right after it prints "CAPTURING". Passive only.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
# shellcheck source=../bin/lan-lib.sh
. "$here/../bin/lan-lib.sh"
secs=120
while [ $# -gt 0 ]; do
  case "$1" in
    --seconds) secs=${2:?--seconds needs a number}; shift 2 ;;
    *) echo "usage: $0 [--seconds N]   (1..300, default 120)" >&2; exit 2 ;;
  esac
done
case "$secs" in ''|*[!0-9]*) echo "FAIL: --seconds must be an integer" >&2; exit 2 ;; esac
[ "$secs" -ge 1 ] && [ "$secs" -le 300 ] || { echo "FAIL: --seconds must be 1..300" >&2; exit 2; }
lan_load AISIS_LAN_FRONT_SSH AISIS_LAN_SPEAKER_ADDR AISIS_LAN_SPEAKER_MAC AISIS_LAN_GATEWAY AISIS_LAN_FRONT_ADDR AISIS_LAN_BACKEND_ADDR

umask 077
out_dir=$HOME/.local/state/aisis-lan/capture
mkdir -p "$out_dir"
log_local=$(mktemp) log_front=$(mktemp)
trap 'rm -f "$log_local" "$log_front"' EXIT

lan_front 'mkdir -p ~/.cache/aisis-lan-capture'
scp -q -o BatchMode=yes "$here/capture.sh" "$AISIS_LAN_FRONT_SSH:.cache/aisis-lan-capture/capture.sh"
env_args="AISIS_LAN_SPEAKER_ADDR=$AISIS_LAN_SPEAKER_ADDR AISIS_LAN_GATEWAY=$AISIS_LAN_GATEWAY"

echo "CAPTURING for $secs s here and on the front host (clocks: here $(date +%T), front $(lan_front date +%T))"
"$here/capture.sh" --seconds "$secs" >"$log_local" 2>&1 &
pid_local=$!
lan_front "$env_args bash ~/.cache/aisis-lan-capture/capture.sh --seconds $secs" >"$log_front" 2>&1 &
pid_front=$!
rc=0
wait "$pid_local" || rc=1
wait "$pid_front" || rc=1
tail -n 3 "$log_local" | sed 's/^/[here]  /'
tail -n 3 "$log_front" | sed 's/^/[front] /'
[ "$rc" -eq 0 ] || { echo "FAIL: a capture failed (see above)" >&2; exit 1; }

local_file=$(sed -n 's/^CAPTURE_FILE=//p' "$log_local")
front_remote=$(sed -n 's/^CAPTURE_FILE=//p' "$log_front")
front_file=$out_dir/front-$(basename "$front_remote")
scp -q -o BatchMode=yes "$AISIS_LAN_FRONT_SSH:$front_remote" "$front_file"
chmod 600 "$front_file"
echo
AISIS_LAN_SPEAKER_MAC=$AISIS_LAN_SPEAKER_MAC AISIS_LAN_FRONT_ADDR=$AISIS_LAN_FRONT_ADDR AISIS_LAN_BACKEND_ADDR=$AISIS_LAN_BACKEND_ADDR \
  python3 -B "$here/summarize.py" "$local_file" "$front_file"
echo "files: $local_file  $front_file"

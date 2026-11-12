#!/usr/bin/env bash
# Time-boxed PASSIVE capture of DHCP (udp 67/68, every host) and the speaker's DNS (speaker address + port 53).
# NOT run by default: use it to find out which DHCP server a client really talks to and which resolver it queries.
# Runs on any host with docker (the workstation and the LAN front use the same script):
#     AISIS_LAN_SPEAKER_ADDR=<ip> AISIS_LAN_GATEWAY=<ip> capture.sh [--seconds N]      N default 120, max 300
# (without those variables the private lan.env is used when present). tcpdump runs in a throwaway container
# (--network host, caps NET_RAW/NET_ADMIN plus SETUID/SETGID to drop root, everything else dropped); its pcap goes to
# stdout and is redirected to a file owned by you, mode 600:  ~/.local/state/aisis-lan/capture/<host>-<timestamp>.pcap
# The last output line is "CAPTURE_FILE=<path>". It changes no config, lease, relay or container.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)

secs=120
while [ $# -gt 0 ]; do
  case "$1" in
    --seconds) secs=${2:?--seconds needs a number}; shift 2 ;;
    *) echo "usage: $0 [--seconds N]   (N: 1..300, default 120)" >&2; exit 2 ;;
  esac
done
case "$secs" in ''|*[!0-9]*) echo "FAIL: --seconds must be an integer" >&2; exit 2 ;; esac
if [ "$secs" -lt 1 ] || [ "$secs" -gt 300 ]; then echo "FAIL: --seconds must be 1..300 (got $secs)" >&2; exit 2; fi

if [ -z "${AISIS_LAN_SPEAKER_ADDR:-}" ] || [ -z "${AISIS_LAN_GATEWAY:-}" ]; then
  # shellcheck source=../bin/lan-lib.sh
  . "$here/../bin/lan-lib.sh"
  lan_load AISIS_LAN_SPEAKER_ADDR AISIS_LAN_GATEWAY
fi
[[ $AISIS_LAN_SPEAKER_ADDR =~ ^[0-9.]+$ && $AISIS_LAN_GATEWAY =~ ^[0-9.]+$ ]] || { echo "FAIL: speaker/gateway must be IPv4 addresses" >&2; exit 2; }

IMAGE=${AISIS_LAN_CAPTURE_IMAGE:-aisis-lan-tcpdump:alpine3.21}
FILTER="(udp port 67 or udp port 68) or (host $AISIS_LAN_SPEAKER_ADDR and port 53)"
umask 077
out_dir=$HOME/.local/state/aisis-lan/capture
mkdir -p "$out_dir"
chmod 700 "$out_dir"

iface=$(ip -4 route get "$AISIS_LAN_GATEWAY" | awk '{for (i = 1; i <= NF; i++) if ($i == "dev") { print $(i + 1); exit }}')
[ -n "$iface" ] || { echo "FAIL: cannot find the interface towards the gateway" >&2; exit 1; }

if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
  echo "building $IMAGE (alpine + tcpdump, once)"
  printf 'FROM alpine:3.21\nRUN apk add --no-cache tcpdump\n' | docker build -q -t "$IMAGE" - >/dev/null
fi

file=$out_dir/$(hostname -s)-$(date +%Y%m%d-%H%M%S).pcap
name=aisis-lan-capture-$$
trap 'docker rm -f "$name" >/dev/null 2>&1 || true' EXIT
echo "capturing $secs s on $(hostname -s) $iface; filter: $FILTER"
# busybox timeout sends SIGTERM -> tcpdump flushes and exits; the outer timeout is the hard stop.
# --log-driver none: the pcap stream on stdout must not be copied into journald/syslog by the host's docker daemon
timeout $((secs + 20)) docker run --rm --name "$name" --network host --log-driver none \
  --cap-drop ALL --cap-add NET_RAW --cap-add NET_ADMIN --cap-add SETUID --cap-add SETGID "$IMAGE" \
  sh -c "timeout $secs tcpdump -i $iface -nn -s 0 -U -w - '$FILTER'" > "$file" || true
chmod 600 "$file"
size=$(stat -c %s "$file")
[ "$size" -ge 24 ] || { echo "FAIL: capture file is empty/invalid ($size bytes): $file" >&2; exit 1; }
echo "done: $size bytes"
echo "CAPTURE_FILE=$file"

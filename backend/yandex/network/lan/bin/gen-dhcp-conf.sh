#!/usr/bin/env bash
# Render the dnsmasq DHCP config for the LAN front from config/dnsmasq-dhcp.conf.template and the private lan.env.
#
#     gen-dhcp-conf.sh                 print the rendered config to stdout
#     gen-dhcp-conf.sh --out FILE      write it (mode 600)
# The fixed-lease list (AISIS_LAN_DHCP_HOSTS_FILE, optional) may only contain `dhcp-host=` lines of the form
# <mac>,[set:tag,]<ipv4>[,<name>]; anything else is refused so the private file cannot inject other dnsmasq options.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
# shellcheck source=lan-lib.sh
. "$here/lan-lib.sh"

out=""
case "${1:-}" in
  "") ;;
  --out) out=${2:?--out needs a file} ;;
  *) echo "usage: $0 [--out FILE]" >&2; exit 2 ;;
esac

lan_load AISIS_LAN_FRONT_IFACE AISIS_LAN_DHCP_RANGE_START AISIS_LAN_DHCP_RANGE_END AISIS_LAN_DHCP_NETMASK \
  AISIS_LAN_DHCP_LEASE_TIME AISIS_LAN_GATEWAY AISIS_LAN_UPSTREAM_DNS AISIS_LAN_RESOLVER AISIS_LAN_SPEAKER_MAC AISIS_LAN_SPEAKER_ADDR
[[ $AISIS_LAN_FRONT_IFACE =~ ^[A-Za-z0-9_.-]+$ ]] || lan_die "AISIS_LAN_FRONT_IFACE has unexpected characters"

# word-split and rejoin: leading/trailing spaces or tabs in the list must not become ",," or embedded junk
upstream=$(printf '%s\n' $AISIS_LAN_UPSTREAM_DNS | paste -sd, -)
speaker_mac=$(echo "$AISIS_LAN_SPEAKER_MAC" | tr 'A-F' 'a-f')
rendered=$(sed \
  -e "s|@IFACE@|$AISIS_LAN_FRONT_IFACE|g" -e "s|@RANGE_START@|$AISIS_LAN_DHCP_RANGE_START|g" \
  -e "s|@RANGE_END@|$AISIS_LAN_DHCP_RANGE_END|g" -e "s|@NETMASK@|$AISIS_LAN_DHCP_NETMASK|g" \
  -e "s|@LEASE_TIME@|$AISIS_LAN_DHCP_LEASE_TIME|g" -e "s|@GATEWAY@|$AISIS_LAN_GATEWAY|g" \
  -e "s|@UPSTREAM_DNS@|$upstream|g" -e "s|@RESOLVER@|$AISIS_LAN_RESOLVER|g" \
  -e "s|@SPEAKER_MAC@|$speaker_mac|g" -e "s|@SPEAKER_ADDR@|$AISIS_LAN_SPEAKER_ADDR|g" \
  "$here/../config/dnsmasq-dhcp.conf.template")

hosts=""
declare -A seen_macs=() seen_ips=()
seen_ips[$AISIS_LAN_SPEAKER_ADDR]=template  # the template pins the speaker's address: no private lease may reuse it
if [ -n "${AISIS_LAN_DHCP_HOSTS_FILE:-}" ]; then
  [ -r "$AISIS_LAN_DHCP_HOSTS_FILE" ] || lan_die "AISIS_LAN_DHCP_HOSTS_FILE is set but not readable"
  n=0
  while IFS= read -r line || [ -n "$line" ]; do
    n=$((n + 1))
    line=${line%%#*}; line=${line%"${line##*[![:space:]]}"}
    [ -n "$line" ] || continue
    [[ $line =~ ^dhcp-host=([0-9a-fA-F]{2}(:[0-9a-fA-F]{2}){5}),(set:[A-Za-z0-9_-]+,)?([0-9]{1,3}(\.[0-9]{1,3}){3})(,[A-Za-z0-9._-]+)?$ ]] \
      || lan_die "line $n of the fixed-lease list is not a plain dhcp-host=<mac>,[set:tag,]<ip>[,<name>] entry"
    mac=$(echo "${BASH_REMATCH[1]}" | tr 'A-F' 'a-f')
    ip=${BASH_REMATCH[4]}
    lan_is_ipv4 "$ip" || lan_die "line $n of the fixed-lease list is not a valid IPv4 address: $ip"
    [ "$mac" != "$speaker_mac" ] || continue  # the speaker lease comes from the template, not the private list
    [ -z "${seen_macs[$mac]:-}" ] || lan_die "line $n of the fixed-lease list repeats the MAC of line ${seen_macs[$mac]}"
    [ -z "${seen_ips[$ip]:-}" ] || lan_die "line $n of the fixed-lease list repeats the address $ip of line ${seen_ips[$ip]}: dnsmasq refuses duplicate dhcp-host addresses"
    seen_macs[$mac]=$n
    seen_ips[$ip]=$n
    hosts+="$line"$'\n'
  done < "$AISIS_LAN_DHCP_HOSTS_FILE"
fi

if command -v dnsmasq >/dev/null 2>&1; then
  # catch a malformed render before it can restart-loop the DHCP container at cutover
  check=$(mktemp)
  { printf '%s\n' "$rendered"; printf '%s' "$hosts"; } > "$check"
  dnsmasq --test --conf-file="$check" >/dev/null 2>&1 || { rm -f "$check"; lan_die "dnsmasq --test rejected the rendered DHCP config"; }
  rm -f "$check"
  echo "    dnsmasq --test: syntax OK" >&2
else
  echo "    WARNING: no dnsmasq here: the rendered config was NOT syntax-checked; check it on the front before cutover" >&2
fi
if [ -n "$out" ]; then
  umask 077
  { printf '%s\n' "$rendered"; printf '%s' "$hosts"; } > "$out.new"
  mv "$out.new" "$out"
else
  printf '%s\n' "$rendered"; printf '%s' "$hosts"
fi

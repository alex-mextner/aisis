#!/usr/bin/env bash
# Tests for the private-config loader/validator and the DHCP config generator (synthetic values only).
#   bash test-config.sh
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
lan=$(cd "$here/.." && pwd)
T=$(mktemp -d)
trap 'rm -rf "$T"' EXIT
fail=0
check() { if [ "$2" = "$3" ]; then echo "PASS $1"; else echo "FAIL $1: got [$2] want [$3]"; fail=1; fi; }

cp "$lan/config/dhcp-hosts.example.conf" "$T/dhcp-hosts.conf"
sed "s|^AISIS_LAN_DHCP_HOSTS_FILE=.*|AISIS_LAN_DHCP_HOSTS_FILE=$T/dhcp-hosts.conf|" "$lan/config/lan.env.example" > "$T/lan.env"
chmod 600 "$T/lan.env"
export AISIS_LAN_ENV=$T/lan.env
validate() { bash "$lan/bin/lan-validate.sh" 2>&1; }

check "the shipped example validates" "$(validate >/dev/null && echo ok)" ok
check "validator prints key names only" "$(validate | grep -c '192.0.2')" 0

chmod 644 "$T/lan.env"
check "group/other readable config is refused" "$(validate | grep -c 'must not be readable')" 1
chmod 600 "$T/lan.env"

grep -v '^AISIS_LAN_GATEWAY=' "$T/lan.env" > "$T/missing.env"; chmod 600 "$T/missing.env"
check "a missing key is named" "$(AISIS_LAN_ENV=$T/missing.env bash "$lan/bin/lan-validate.sh" 2>&1 | grep -c 'AISIS_LAN_GATEWAY is missing')" 1

sed 's|^AISIS_LAN_SPEAKER_MAC=.*|AISIS_LAN_SPEAKER_MAC=not-a-mac|' "$T/lan.env" > "$T/badmac.env"; chmod 600 "$T/badmac.env"
check "a malformed MAC is named" "$(AISIS_LAN_ENV=$T/badmac.env bash "$lan/bin/lan-validate.sh" 2>&1 | grep -c 'AISIS_LAN_SPEAKER_MAC must be a MAC')" 1

sed 's|^AISIS_LAN_FRONT_ADDR=.*|AISIS_LAN_FRONT_ADDR=300.1.1.1|' "$T/lan.env" > "$T/badip.env"; chmod 600 "$T/badip.env"
check "a malformed IPv4 is named" "$(AISIS_LAN_ENV=$T/badip.env bash "$lan/bin/lan-validate.sh" 2>&1 | grep -c 'AISIS_LAN_FRONT_ADDR must be an IPv4')" 1

check "a missing file is explained" "$(AISIS_LAN_ENV=$T/nope.env bash "$lan/bin/lan-validate.sh" 2>&1 | grep -c 'not found')" 1
check "AISIS_CONFIG_DIR selects the directory" "$(env -u AISIS_LAN_ENV AISIS_CONFIG_DIR="$T" bash "$lan/bin/lan-validate.sh" >/dev/null 2>&1 && echo ok)" ok

echo "# DHCP config generator"
gen() { bash "$lan/bin/gen-dhcp-conf.sh" "$@"; }
out=$(gen)
check "interface rendered" "$(printf '%s\n' "$out" | grep -c '^interface=eth0$')" 1
check "range rendered" "$(printf '%s\n' "$out" | grep -c '^dhcp-range=192.0.2.100,192.0.2.200,255.255.255.0,3600s$')" 1
check "router option" "$(printf '%s\n' "$out" | grep -c '^dhcp-option=option:router,192.0.2.1$')" 1
check "everyone but the speaker gets the upstream DNS" "$(printf '%s\n' "$out" | grep -c '^dhcp-option=tag:!speaker,option:dns-server,198.51.100.1,198.51.100.2,198.51.100.3$')" 1
check "the speaker gets the front resolver" "$(printf '%s\n' "$out" | grep -c '^dhcp-option=tag:speaker,option:dns-server,192.0.2.10$')" 1
check "speaker pin tagged" "$(printf '%s\n' "$out" | grep -c '^dhcp-host=02:00:00:00:00:30,set:speaker,192.0.2.30$')" 1
check "fixed leases from the private list" "$(printf '%s\n' "$out" | grep -c '^dhcp-host=02:00:00:00:00:10,192.0.2.10,front-host$')" 1

sed 's|^AISIS_LAN_UPSTREAM_DNS=.*|AISIS_LAN_UPSTREAM_DNS=" 198.51.100.1   198.51.100.2 "|' "$T/lan.env" > "$T/spacey.env"; chmod 600 "$T/spacey.env"
check "extra whitespace in the upstream DNS list renders clean" "$(AISIS_LAN_ENV=$T/spacey.env gen | grep -c '^dhcp-option=tag:!speaker,option:dns-server,198.51.100.1,198.51.100.2$')" 1

printf 'dhcp-host=02:00:00:00:00:30,192.0.2.99  # an old pin for the speaker is dropped\ndhcp-host=02:00:00:00:00:40,192.0.2.40\n' > "$T/dhcp-hosts.conf"
out=$(gen)
check "a pin for the speaker's MAC in the list is not duplicated" "$(printf '%s\n' "$out" | grep -c '^dhcp-host=02:00:00:00:00:30')" 1
check "other pins kept" "$(printf '%s\n' "$out" | grep -c '^dhcp-host=02:00:00:00:00:40,192.0.2.40$')" 1

printf 'dhcp-script=/bin/evil\n' > "$T/dhcp-hosts.conf"
check "any other dnsmasq directive in the list is refused" "$(gen >/dev/null 2>&1 && echo accepted || echo refused)" refused
printf 'dhcp-host=02:00:00:00:00:50,192.0.2.50,name with space\n' > "$T/dhcp-hosts.conf"
check "a malformed pin is refused" "$(gen >/dev/null 2>&1 && echo accepted || echo refused)" refused
cp "$lan/config/dhcp-hosts.example.conf" "$T/dhcp-hosts.conf"

gen --out "$T/rendered.conf"
check "--out writes mode 600" "$(stat -c %a "$T/rendered.conf")" 600
if command -v dnsmasq >/dev/null; then
  check "dnsmasq accepts the rendered file" "$(dnsmasq --test --conf-file="$T/rendered.conf" 2>&1 | grep -c 'syntax check OK')" 1
else
  echo "SKIP dnsmasq --test (dnsmasq not installed)"
fi
exit "$fail"

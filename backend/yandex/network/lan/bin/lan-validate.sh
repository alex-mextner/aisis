#!/usr/bin/env bash
# Validate the private LAN inventory (~/.config/aisis/lan.env or $AISIS_CONFIG_DIR/lan.env).
#     lan-validate.sh            fail with a clear message on the first missing/malformed value
# Prints only key names, never values.
set -euo pipefail
# shellcheck source=lan-lib.sh
. "$(dirname "$0")/lan-lib.sh"

KEYS=(
  AISIS_LAN_FRONT_ADDR AISIS_LAN_FRONT_SSH AISIS_LAN_FRONT_IFACE
  AISIS_LAN_BACKEND_ADDR AISIS_LAN_BACKEND_PORTS AISIS_LAN_BACKEND_UPSTREAM_PORT AISIS_LAN_GATEWAY
  AISIS_LAN_SPEAKER_ADDR AISIS_LAN_SPEAKER_MAC AISIS_LAN_SPEAKER_ADB AISIS_LAN_RESOLVER
  AISIS_LAN_UPSTREAM_DNS AISIS_LAN_DHCP_RANGE_START AISIS_LAN_DHCP_RANGE_END AISIS_LAN_DHCP_NETMASK AISIS_LAN_DHCP_LEASE_TIME
  AISIS_LAN_PROJECT AISIS_LAN_FRONT_DIR AISIS_LAN_RELAY_CONFIG_DIR AISIS_LAN_VOICE_DOMAINS_DIR
)
lan_load "${KEYS[@]}"
for key in "${KEYS[@]}"; do lan_ok "$key"; done
if [ -n "${AISIS_LAN_DHCP_HOSTS_FILE:-}" ] && [ ! -r "$AISIS_LAN_DHCP_HOSTS_FILE" ]; then
  lan_die "AISIS_LAN_DHCP_HOSTS_FILE is set but not readable"
fi
lan_say "$LAN_ENV is complete"

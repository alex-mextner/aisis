#!/usr/bin/env bash
# Shared helpers for the LAN front scripts (sourced, never executed). No personal values live here: they come from the
# owner-only $AISIS_CONFIG_DIR/lan.env (default ~/.config/aisis/lan.env); see config/lan.env.example for the schema.

AISIS_CONFIG_DIR=${AISIS_CONFIG_DIR:-$HOME/.config/aisis}
LAN_ENV=${AISIS_LAN_ENV:-$AISIS_CONFIG_DIR/lan.env}

lan_say() { echo "==> $*"; }
lan_ok() { echo "    ok: $*"; }
lan_die() { echo "FAIL: $*" >&2; exit 1; }

lan_is_ipv4() { [[ $1 =~ ^((25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9]?[0-9])\.){3}(25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9]?[0-9])$ ]]; }
lan_is_mac() { [[ $1 =~ ^([0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}$ ]]; }
lan_is_port() { [[ $1 =~ ^[0-9]+$ ]] && [ "$1" -ge 1 ] && [ "$1" -le 65535 ]; }
lan_is_hostport() { [[ $1 =~ ^[^:[:space:]]+:[0-9]+$ ]] && lan_is_port "${1##*:}"; }

# Format check for one key; unknown keys are free text (non-empty, no whitespace unless the schema says list).
lan_check_key() {
  local key=$1 value=${!1:-} item
  case $key in
    AISIS_LAN_FRONT_ADDR|AISIS_LAN_BACKEND_ADDR|AISIS_LAN_GATEWAY|AISIS_LAN_SPEAKER_ADDR|AISIS_LAN_RESOLVER|\
    AISIS_LAN_DHCP_RANGE_START|AISIS_LAN_DHCP_RANGE_END|AISIS_LAN_DHCP_NETMASK)
      lan_is_ipv4 "$value" || lan_die "$key must be an IPv4 address, got '$value' ($LAN_ENV)" ;;
    AISIS_LAN_SPEAKER_MAC)
      lan_is_mac "$value" || lan_die "$key must be a MAC like 02:00:00:00:00:30, got '$value' ($LAN_ENV)" ;;
    AISIS_LAN_UPSTREAM_DNS)
      for item in $value; do lan_is_ipv4 "$item" || lan_die "$key must be a list of IPv4 addresses, bad item '$item' ($LAN_ENV)"; done ;;
    AISIS_LAN_BACKEND_PORTS)
      for item in $value; do lan_is_port "$item" || lan_die "$key must be a list of TCP ports, bad item '$item' ($LAN_ENV)"; done ;;
    AISIS_LAN_BACKEND_UPSTREAM_PORT)
      lan_is_port "$value" || lan_die "$key must be a TCP port, got '$value' ($LAN_ENV)" ;;
    AISIS_LAN_SPEAKER_ADB)
      lan_is_hostport "$value" || lan_die "$key must be HOST:PORT, got '$value' ($LAN_ENV)" ;;
    AISIS_LAN_DHCP_LEASE_TIME)
      [[ $value =~ ^[0-9]+[smhd]?$ ]] || lan_die "$key must look like 3600s / 1h, got '$value' ($LAN_ENV)" ;;
    AISIS_LAN_FRONT_DIR)
      [[ $value != /* ]] || lan_die "$key must be relative to the front host's \$HOME, got '$value' ($LAN_ENV)" ;;
    AISIS_LAN_RELAY_CONFIG_DIR|AISIS_LAN_VOICE_DOMAINS_DIR)
      [[ $value == /* ]] || lan_die "$key must be an absolute path on the front host, got '$value' ($LAN_ENV)" ;;
  esac
}

# lan_load KEY...: source lan.env (must be owner-only) and require every named KEY to be set and well formed.
lan_load() {
  [ -f "$LAN_ENV" ] || lan_die "private config $LAN_ENV not found (copy config/lan.env.example there, mode 600)"
  local mode
  mode=$(stat -L -c %a "$LAN_ENV")
  [ "$(( 8#$mode & 8#077 ))" -eq 0 ] || lan_die "$LAN_ENV must not be readable by group/others (chmod 600 it, mode is $mode)"
  set -a
  # shellcheck disable=SC1090
  . "$LAN_ENV"
  set +a
  : "${AISIS_LAN_RESOLVER:=${AISIS_LAN_FRONT_ADDR:-}}"
  local key
  for key in "$@"; do
    [ -n "${!key:-}" ] || lan_die "$key is missing in $LAN_ENV (schema: config/lan.env.example)"
    lan_check_key "$key"
  done
}

# Run a command on the always-on LAN host (non-interactive ssh).
lan_front() { ssh -o BatchMode=yes -o ConnectTimeout=8 "$AISIS_LAN_FRONT_SSH" "$@"; }

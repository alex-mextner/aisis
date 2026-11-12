#!/usr/bin/env bash
# Install / refresh the LAN front's files on the always-on host from this repository. Run from the workstation.
#
#     front-install.sh              sync files, render front.env and the DHCP config, validate, show what `up` would change
#     front-install.sh --apply      additionally run `up -d dns-relay tls-voice` (DHCP and the watcher are never started here)
#
# What it installs under <front dir> (AISIS_LAN_FRONT_DIR, relative to the front host's $HOME):
#   release/compose.network.yaml, release/network/{dns_forwarder.py,probe_dns.py}   copied unedited from the repository
#   compose.front.yaml, dc, watcher/, dhcp/{Dockerfile,dnsmasq-dhcp.conf}                    from this directory
#   front.env (mode 600)                                                           rendered from the private lan.env
# It does NOT touch the owner-only relay config directory, the TLS proxy checkout <front dir>/proxy-src (the build context
# that holds the certificates; only needed to build the TLS image) or the per-list domain files: those are the owner's
# inventory and must already exist (it checks the config and domain files and fails clearly). The DHCP container is never started here.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
lan=$(cd "$here/.." && pwd)
yandex=$(cd "$lan/../.." && pwd)
# shellcheck source=lan-lib.sh
. "$here/lan-lib.sh"

apply=0
case "${1:-}" in
  "") ;;
  --apply) apply=1 ;;
  *) echo "usage: $0 [--apply]" >&2; exit 2 ;;
esac

lan_load AISIS_LAN_FRONT_SSH AISIS_LAN_FRONT_ADDR AISIS_LAN_FRONT_DIR AISIS_LAN_PROJECT AISIS_LAN_RELAY_CONFIG_DIR \
  AISIS_LAN_VOICE_DOMAINS_DIR AISIS_LAN_BACKEND_ADDR AISIS_LAN_BACKEND_PORTS AISIS_LAN_BACKEND_UPSTREAM_PORT
[[ $AISIS_LAN_PROJECT =~ ^[a-z0-9][a-z0-9_-]*$ ]] || lan_die "AISIS_LAN_PROJECT must be lowercase letters, digits, - or _"

lan_say "front host"
lan_front true || lan_die "cannot ssh to the front host"
# shellcheck disable=SC2016  # $HOME must expand on the front host, not here
home=$(lan_front 'printf %s "$HOME"')
uid=$(lan_front 'id -u')
gid=$(lan_front 'id -g')
dest=$home/$AISIS_LAN_FRONT_DIR
state=$home/.local/state/$AISIS_LAN_PROJECT
lan_ok "reachable, installing under $AISIS_LAN_FRONT_DIR"

lan_say "owner-only inputs must already exist on the front host"
lan_front "[ -f '$AISIS_LAN_RELAY_CONFIG_DIR/config.json' ] && [ -f '$AISIS_LAN_RELAY_CONFIG_DIR/voice-nginx.conf' ]" \
  || lan_die "relay config.json/voice-nginx.conf missing in $AISIS_LAN_RELAY_CONFIG_DIR (render them with network/prepare_local.py)"
lan_front "ls '$AISIS_LAN_VOICE_DOMAINS_DIR'/*.txt >/dev/null 2>&1" \
  || lan_die "no *.txt domain files in $AISIS_LAN_VOICE_DOMAINS_DIR on the front host"
lan_ok "relay config and domain files present"

lan_say "sync files"
lan_front "umask 077; mkdir -p '$dest/release/network' '$dest/watcher' '$dest/dhcp' '$state/dhcp'; chmod 755 '$state' '$state/dhcp'"
rsync -a -e "ssh -o BatchMode=yes" "$yandex/compose.network.yaml" "$AISIS_LAN_FRONT_SSH:$dest/release/"
rsync -a -e "ssh -o BatchMode=yes" "$yandex/network/dns_forwarder.py" "$yandex/network/probe_dns.py" "$AISIS_LAN_FRONT_SSH:$dest/release/network/"
rsync -a -e "ssh -o BatchMode=yes" "$lan/compose/compose.front.yaml" "$AISIS_LAN_FRONT_SSH:$dest/"
rsync -a -e "ssh -o BatchMode=yes" "$lan/bin/dc" "$AISIS_LAN_FRONT_SSH:$dest/dc"
rsync -a -e "ssh -o BatchMode=yes" "$lan/watcher/backend_watcher.py" "$AISIS_LAN_FRONT_SSH:$dest/watcher/"
rsync -a -e "ssh -o BatchMode=yes" "$lan/compose/dhcp.Dockerfile" "$AISIS_LAN_FRONT_SSH:$dest/dhcp/Dockerfile"
lan_ok "files synced"

lan_say "render front.env and the DHCP config (mode 600)"
targets=""
for port in $AISIS_LAN_BACKEND_PORTS; do targets+="${targets:+ }$AISIS_LAN_BACKEND_ADDR:$port"; done
tmp=$(mktemp)
trap 'rm -f "$tmp" "$tmp.dhcp"' EXIT
{
  echo "# Rendered by front-install.sh from the private lan.env; do not edit by hand."
  echo "AISIS_LAN_PROJECT=$AISIS_LAN_PROJECT"
  echo "AISIS_LAN_HOME=$dest"
  echo "AISIS_LAN_STATE_DIR=$state"
  echo "AISIS_NETWORK_CONFIG_DIR=$AISIS_LAN_RELAY_CONFIG_DIR"
  echo "AISIS_SPEAKER_BIND_IP=$AISIS_LAN_FRONT_ADDR"
  echo "AISIS_PROXY_SOURCE=$dest/proxy-src"
  echo "AISIS_LAN_BACKEND_ADDR=$AISIS_LAN_BACKEND_ADDR"
  echo "AISIS_LAN_BACKEND_UPSTREAM_PORT=$AISIS_LAN_BACKEND_UPSTREAM_PORT"
  echo "AISIS_LAN_BACKEND_TARGETS=\"$targets\""
  echo "AISIS_LAN_VOICE_DOMAINS_DIR=$AISIS_LAN_VOICE_DOMAINS_DIR"
  echo "AISIS_LAN_UID=$uid"
  echo "AISIS_LAN_GID=$gid"
  [ -z "${AISIS_LAN_TLS_IMAGE:-}" ] || echo "AISIS_LAN_TLS_IMAGE=$AISIS_LAN_TLS_IMAGE"
  [ -z "${AISIS_LAN_DHCP_IMAGE:-}" ] || echo "AISIS_LAN_DHCP_IMAGE=$AISIS_LAN_DHCP_IMAGE"
} > "$tmp"
"$here/gen-dhcp-conf.sh" --out "$tmp.dhcp"
scp -q -o BatchMode=yes "$tmp" "$AISIS_LAN_FRONT_SSH:$dest/front.env"
scp -q -o BatchMode=yes "$tmp.dhcp" "$AISIS_LAN_FRONT_SSH:$dest/dhcp/dnsmasq-dhcp.conf"
lan_front "chmod 600 '$dest/front.env' '$dest/dhcp/dnsmasq-dhcp.conf'"
lan_ok "front.env and dhcp/dnsmasq-dhcp.conf installed"

lan_say "validate the compose project (all profiles)"
lan_front "'$dest/dc' --profile dhcp --profile watcher config -q" || lan_die "docker compose config failed on the front host"
lan_ok "compose config is valid"

lan_say "what \`up -d dns-relay tls-voice\` would change (dry run)"
if ! lan_front "set -o pipefail; '$dest/dc' --dry-run --profile voice up -d dns-relay tls-voice 2>&1 | sed 's/^/    /'"; then
  echo "    FAIL: the dry run itself failed; read the message above before applying" >&2
fi

if [ "$apply" -eq 1 ]; then
  lan_say "apply"
  # --force-recreate: bind-mounted code/config (dns_forwarder.py, nginx templates) is not part of Compose's
  # up-to-date check, so without it a plain `up -d` leaves the old processes running after a sync
  lan_front "'$dest/dc' --profile voice up -d --force-recreate dns-relay tls-voice"
  lan_ok "relay and TLS front recreated (forced, so synced bind-mount changes are loaded)"
else
  echo "    (not applied; re-run with --apply to create/recreate dns-relay and tls-voice)"
fi

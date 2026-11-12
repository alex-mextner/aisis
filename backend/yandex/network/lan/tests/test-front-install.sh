#!/usr/bin/env bash
# Test bin/front-install.sh against a fake front host: stub ssh/rsync/scp/docker put the "front" in a temp dir, so the
# files it installs, the rendered front.env and the compose calls can be inspected. Synthetic values only.
#   bash test-front-install.sh
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
lan=$(cd "$here/.." && pwd)
T=$(mktemp -d)
trap 'rm -rf "$T"' EXIT
fail=0
check() { if [ "$2" = "$3" ]; then echo "PASS $1"; else echo "FAIL $1: got [$2] want [$3]"; fail=1; fi; }

FRONT_HOME=$T/fronthome
mkdir -p "$T/bin" "$FRONT_HOME/priv/voice-domains"
echo '{"enabled": true}' > "$FRONT_HOME/priv/config.json"
echo 'server {}' > "$FRONT_HOME/priv/voice-nginx.conf"
echo 'example.test' > "$FRONT_HOME/priv/voice-domains/a.txt"

# stubs: ssh runs the remote command locally with HOME = the fake front; rsync/scp strip the "host:" prefix
cat > "$T/bin/ssh" <<'SH'
#!/usr/bin/env bash
while [ $# -gt 0 ]; do case "$1" in -o) shift 2 ;; -*) shift ;; *) break ;; esac; done
shift  # the host
HOME=$FRONT_HOME PATH=$STUB_BIN:$PATH bash -c "$*"
SH
cat > "$T/bin/rsync" <<'SH'
#!/usr/bin/env bash
args=()
while [ $# -gt 0 ]; do case "$1" in -e) shift 2 ;; -*) args+=("$1"); shift ;; *) args+=("${1#front-host:}"); shift ;; esac; done
exec /usr/bin/rsync "${args[@]}"
SH
cat > "$T/bin/scp" <<'SH'
#!/usr/bin/env bash
args=()
while [ $# -gt 0 ]; do case "$1" in -o) shift 2 ;; -q) shift ;; *) args+=("${1#front-host:}"); shift ;; esac; done
exec cp "${args[@]}"
SH
cat > "$T/bin/docker" <<'SH'
#!/usr/bin/env bash
echo "docker $*" >> "$DOCKER_LOG"
exit 0
SH
chmod +x "$T/bin"/*
export STUB_BIN=$T/bin FRONT_HOME DOCKER_LOG=$T/docker.log
export PATH=$T/bin:$PATH

cp "$lan/config/dhcp-hosts.example.conf" "$T/dhcp-hosts.conf"
sed -e "s|^AISIS_LAN_DHCP_HOSTS_FILE=.*|AISIS_LAN_DHCP_HOSTS_FILE=$T/dhcp-hosts.conf|" \
    -e "s|^AISIS_LAN_FRONT_SSH=.*|AISIS_LAN_FRONT_SSH=front-host|" \
    -e "s|^AISIS_LAN_RELAY_CONFIG_DIR=.*|AISIS_LAN_RELAY_CONFIG_DIR=$FRONT_HOME/priv|" \
    -e "s|^AISIS_LAN_VOICE_DOMAINS_DIR=.*|AISIS_LAN_VOICE_DOMAINS_DIR=$FRONT_HOME/priv/voice-domains|" \
    -e "s|^#AISIS_LAN_TLS_IMAGE=.*|AISIS_LAN_TLS_IMAGE=example-https:test|" \
    "$lan/config/lan.env.example" > "$T/lan.env"
chmod 600 "$T/lan.env"
export AISIS_LAN_ENV=$T/lan.env
D=$FRONT_HOME/.local/share/aisis-lan

bash "$lan/bin/front-install.sh" >"$T/out.log" 2>&1 || { cat "$T/out.log"; fail=1; }
check "compose file synced" "$([ -f "$D/release/compose.network.yaml" ] && echo yes)" yes
check "relay code synced" "$([ -f "$D/release/network/dns_forwarder.py" ] && [ -f "$D/release/network/probe_dns.py" ] && echo yes)" yes
check "override, wrapper and watcher synced" "$([ -f "$D/compose.front.yaml" ] && [ -x "$D/dc" ] && [ -f "$D/watcher/backend_watcher.py" ] && echo yes)" yes
check "no caddy directory installed" "$([ -e "$D/caddy" ] && echo present || echo absent)" absent
check "front.env is owner-only" "$(stat -c %a "$D/front.env")" 600
check "front.env project" "$(grep -c '^AISIS_LAN_PROJECT=aisis-lan$' "$D/front.env")" 1
check "front.env backend targets built from addr x ports" "$(grep -c '^AISIS_LAN_BACKEND_TARGETS="192.0.2.20:31115 192.0.2.20:18080"$' "$D/front.env")" 1
check "front.env points the TLS build context at the owner-provided checkout" "$(grep -c "^AISIS_PROXY_SOURCE=$D/proxy-src\$" "$D/front.env")" 1
check "front.env bind address" "$(grep -c '^AISIS_SPEAKER_BIND_IP=192.0.2.10$' "$D/front.env")" 1
check "front.env optional image only when set" "$(grep -c '^AISIS_LAN_TLS_IMAGE=example-https:test$' "$D/front.env")$(grep -c 'AISIS_LAN_DHCP_IMAGE' "$D/front.env")" "10"
check "front.env carries the ssh user's uid" "$(grep -c "^AISIS_LAN_UID=$(id -u)\$" "$D/front.env")" 1
check "dhcp config installed owner-only" "$(stat -c %a "$D/dhcp/dnsmasq-dhcp.conf")" 600
check "dhcp config has the generated speaker pin" "$(grep -c '^dhcp-host=02:00:00:00:00:30,set:speaker,192.0.2.30$' "$D/dhcp/dnsmasq-dhcp.conf")" 1
check "private relay config untouched" "$(cat "$FRONT_HOME/priv/config.json")" '{"enabled": true}'
check "compose config validated with every profile" "$(grep -c 'config -q' "$DOCKER_LOG")" 1
check "the dry run uses the global --dry-run flag before the subcommand" "$(grep -c -- ' --dry-run --profile voice up -d dns-relay tls-voice$' "$DOCKER_LOG")" 1
check "no container was started without --apply (dry run only)" "$(grep ' up -d ' "$DOCKER_LOG" | grep -vc -- '--dry-run')" 0
check "only documentation addresses in the installed files" "$(grep -rhoE '[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+' "$D" | sort -u | grep -vcE '^(192\.0\.2\.|198\.51\.100\.|203\.0\.113\.|127\.|0\.|255\.|10\.[0-9]+\.[0-9]+\.[0-9]+$)' || true)" 0
: > "$DOCKER_LOG"
bash "$lan/bin/front-install.sh" --apply >"$T/out2.log" 2>&1 || { cat "$T/out2.log"; fail=1; }
check "--apply force-recreates the relay and the TLS front (bind-mount updates)" "$(grep -c -- '--profile voice up -d --force-recreate dns-relay tls-voice$' "$DOCKER_LOG")" 1
check "--apply never starts dhcp or the watcher" "$(grep -c 'up -d .*\(dhcp\|backend-watcher\)' "$DOCKER_LOG")" 0

rm -f "$FRONT_HOME/priv/config.json"
check "missing relay config is a clear failure" "$(bash "$lan/bin/front-install.sh" 2>&1 | grep -c 'config.json/voice-nginx.conf missing')" 1
exit "$fail"

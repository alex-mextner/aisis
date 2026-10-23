# Scoped speaker DNS and TLS compatibility check

This optional stack initially forwards DNS normally. It changes no router settings at startup.
The public example uses documentation addresses and synthetic MACs; real inventory belongs in
an owner-only `config.json` outside this checkout.

The first compatibility check redirects only three exact configuration hostnames for one
speaker. Both its source IP and current `/proc/net/arp` MAC must match. Other clients and names
pass through unchanged. There are no blocked endpoints. The TLS compatibility service uses
the pinned proxy's `!domain` mode, forwarding original Yandex responses, including account
configuration, unchanged. It does not enable ADB or feed the replacement backend's account data.

## Private configuration

Copy `config.example.json` to a private directory and replace all documentation addresses and
synthetic MACs. Set the router host, existing router-cli checkout path, server MAC, target
speaker IP/MAC, explicit original DNS upstreams, and administration probe IPs. Keep `enabled`
false initially. The `bind_ip` is the DNS relay's stable LAN address; `route_ip` is the TLS gateway.
Do not use loopback/self DNS as upstreams. Keep the router's existing DHCP pool and lease time.

Run `python3 network/prepare_local.py --config /absolute/private/config.json` from the parent
backend directory. It writes an owner-only Nginx config beside the private JSON and prints the
two environment settings needed below. Use the same exported values for all Compose commands:

```bash
export AISIS_NETWORK_CONFIG_DIR=/absolute/private/network
export AISIS_SPEAKER_BIND_IP=<private-LAN-bind-address>
docker compose -f compose.network.yaml up -d dns-relay
python3 -m unittest discover -s network -p 'test_*.py'
```

The DNS listener binds only the address in private config, TCP and UDP port 53. Nginx defaults
to loopback until `AISIS_SPEAKER_BIND_IP` is explicitly supplied. Its resolver uses only the
private config's original upstreams, preventing recursion through the scoped relay. Target or
upstream changes require re-running `prepare_local.py` and restarting `tls-compat`.

## Verify without changing DHCP DNS

From a separate authorized LAN host, with `probe_dns.py` and `dns_forwarder.py` together, run:

```bash
python3 probe_dns.py --server <private-LAN-bind-address> --upstream <original-DNS-address>
```

This sends four explicit test queries, over UDP and TCP, for a normal public name and a Quasar
name. It does not change the host's resolver settings. The normal client must receive public
addresses in all four cases. The local socket transport test must pass on the actual host; a
skip in a restricted build sandbox is not sufficient runtime verification.

Build/start the optional TLS compatibility service using the already prepared pinned proxy
checkout. It shares LAN ports 80/443 with the regular speaker TLS profile; run only one:

```bash
docker compose -f compose.network.yaml --profile compat up -d --build tls-compat
```

Verify Nginx configuration and a request from an explicitly allowed administration host before
directing speaker traffic. The only compatibility access-log fields are source IP, TLS SNI,
TLS protocol, HTTP status and upstream status. No URI, query, cookies, authorization or body is
logged. Upstream error logging is disabled because error messages can contain request URLs.
Successful HTTP requests from the target speaker are evidence it accepted the TLS chain; a
listener or a curl using disabled certificate checks does not prove speaker compatibility.

## Prepare router plans and backup

Use the existing supported router-cli installation and credential store. These commands never
print credential values. The tool takes the existing local inventory lock and logs out of a
session it opened, including on failure. It does not take over someone else's existing session.
Do not run concurrent router commands from other hosts; the local lock is not distributed.

```bash
python3 network/router_control.py prepare --config /absolute/private/config.json --snapshot /absolute/private/router-before.json
```

`prepare` saves the original three DNS fields and DHCP lease duration and prints dry-run plans
for the server's current-IP reservation and DNS change. It refuses to overwrite the original
snapshot. A backup file is the only persistent change in this step. Review the plans.

The DNS host must have the correct static DHCP reservation before its address is advertised.
Use `reserve` without `--apply` to review; an explicit `--apply` executes only the reservation:

```bash
python3 network/router_control.py reserve --config /absolute/private/config.json --snapshot /absolute/private/router-before.json
```

## Independent rollback before a temporary switch

After host/LAN checks and reservation, print the independent rollback timer command:

```bash
python3 network/prepare_local.py --config /absolute/private/config.json --snapshot /absolute/private/router-before.json --arm-rollback 300
```

Adding `--apply` arms that timer; it still changes no router DNS. The timer uses the existing
private credentials and retries a failed rollback no more frequently than once per 90 seconds.
Only after the timer is confirmed active may an operator explicitly execute `apply-dns --apply`.
The guard also checks current DNS against the saved baseline, the server reservation, TCP/UDP
DNS forwarding, and an available TLS listener. These checks complement the independent LAN
probe and backend review; they do not establish speaker firmware compatibility.

```bash
python3 network/router_control.py apply-dns --config /absolute/private/config.json --snapshot /absolute/private/router-before.json
```

Without `--apply`, this remains a dry run. The router DNS change is global DHCP configuration;
only the relay's answers are device scoped. Run `compat --apply` to enable the single-target
answer override and `forwarding --apply` to disable it. Merely starting this stack leaves it off.
Do not perform the actual backend switch until the compatibility result has been reviewed.

## Rollback and continued normal forwarding

```bash
python3 network/router_control.py rollback --apply --config /absolute/private/config.json --snapshot /absolute/private/router-before.json
```

Rollback first disables the target override, even if the gateway is unreachable. It stops only
`tls-voice` and `tls-compat` through the private `network_compose` path, closing existing proxied
WebSockets so they can reconnect to the original service. It keeps `dns-relay` running. It restores
all three original DNS values only if current settings still match this tool's settings or the
original snapshot; it refuses to overwrite a later unrelated change. It records a
`forwarding-retention.json` deadline next to the snapshot. Keep `dns-relay` running normally for
at least the entire original DHCP lease plus five minutes after restoration (minimum 3900
seconds). Clients may keep DNS addresses issued during the temporary window after the router
itself has been restored. Do not stop the relay abruptly or release its address reservation.

## Voice-only mode after compatibility

The optional `voice` profile forwards only `uniproxy.alice.yandex.net` to the replacement
backend on host port 31115, preserving the domain prefix expected by its WebSocket router.
The three Quasar/scbh hostnames continue forwarding original responses if a short-lived
cached compatibility DNS answer reaches the gateway. The DNS `voice` mode overrides only the
uniproxy hostname; no account, OAuth, GLAGOL or system sync endpoints are selected.

After reviewing actual firmware compatibility and backend readiness, stop `tls-compat` before
starting the mutually exclusive `--profile voice` service `tls-voice`. Enable the single-host
DNS selection with `router_control.py voice --apply --config ...`. Existing WebSockets can
remain open to their old destination until they reconnect; DNS changes alone do not move a
live socket. A real microphone request, backend transcription/response and audible reply must
be observed before claiming voice migration. No ADB activation or firmware flashing is part of
this path. Rollback still disables overrides and restores DHCP DNS as described above.

### Multiple owned voice targets

`voice_targets` is an explicit private list of objects with `ip` and `mac`. Voice mode matches
only these IP/MAC pairs. If the list is absent it falls back to the primary `target`; an IP
whose current ARP MAC differs is forwarded normally. Compatibility mode always remains scoped
to the single primary `target`, regardless of the voice list.

`prepare_local.py` generates `compat-nginx.conf` for the primary target and `voice-nginx.conf`
for the private voice list, plus explicitly configured administration probe IPs. After adding
voice targets, render again and **recreate/restart `tls-voice`** before enabling those clients:
DNS JSON reloads per request, while a running Nginx ACL does not reload automatically. A plain
unchanged Compose `up` can retain the old ACL. Prefer rendering the full owned fleet before
starting the voice profile, then prove the pilot before enabling the remaining speakers.

The rollback's Docker stop is executed through a fresh, noninteractive `ssh localhost` login
with strict host-key checking and a five-second connection timeout. This obtains current
Docker group membership even if the user systemd manager retained old supplementary groups.
There is no direct-Docker fallback and no permission relaxation. Before any DNS write, prove
that exact SSH/Docker route from a transient user-systemd service using a read-only Docker
operation; an interactive SSH success alone does not establish the manager's authentication
environment. SSH failure/timeout is reported as rollback failure, while normal DNS forwarding
and original-DNS restoration still proceed.

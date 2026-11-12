# Always-on LAN front: DHCP, DNS relay and TLS front on one host

Scripts and default configs to run the speaker-facing network services on an always-on LAN host, so the LAN keeps
working while the workstation that runs the recognition backend is off. The backend stays where it is; the front only
hands out addresses, replaces DNS answers for the speaker and forwards the speaker's voice traffic: TLS ends on the
front, and the WebSocket to the backend crosses the LAN unencrypted (known limitation, see the end of this file).
With the backend host off, the voice assistant is down by design; DHCP and DNS for the rest of the LAN keep working.

Everything personal (addresses, MACs, hostnames, fixed leases, relay config, certificates) lives in an owner-only
directory **outside the checkout**: `$AISIS_CONFIG_DIR` (default `~/.config/aisis`). Nothing from there is ever
committed (`../.gitignore` guards the usual names). `config/lan.env.example` documents the schema with documentation
addresses (RFC 5737) and synthetic MACs; `bin/lan-validate.sh` fails clearly on a missing or malformed value. Every
script takes the file from `$AISIS_CONFIG_DIR/lan.env` (override: `AISIS_LAN_ENV=/path/to/lan.env`) and refuses a file that
group/others can read.

## Layout

| path | purpose |
|---|---|
| `config/lan.env.example` | schema of the private `lan.env`; `config/dnsmasq-dhcp.conf.template`, `config/dhcp-hosts.example.conf` |
| `bin/lan-lib.sh`, `bin/lan-validate.sh` | loader/validator used by every script |
| `bin/gen-dhcp-conf.sh` | render the dnsmasq DHCP config from the template, `lan.env` and the private fixed-lease list |
| `bin/front-install.sh`, `bin/dc` | install/refresh the compose project on the front host from this repo; compose wrapper (on the front) |
| `bin/front-static-ip.sh` | pin the front host's address (systemd-networkd; auto-reverts when connectivity fails) |
| `bin/verify.sh` | health + path proof (read-only) |
| `compose/` | compose override (relay/TLS overrides, DHCP, optional watcher), DHCP image |
| `speaker/` | point the speaker's resolver at the front, and undo |
| `watcher/` | OPTIONAL fail-open relay watcher (see the warning below) |
| `failover/` | make the workstation's failover watchdog skip components that run on the front |
| `capture/` | passive tcpdump kit to find out which DHCP server / resolver a client really uses (NOT run by default) |
| `tests/` | rootless tests for the scripts above (fake hosts, synthetic values) |

## Order

1. **Private config.** Copy `config/lan.env.example` to `~/.config/aisis/lan.env` (mode 600), fill in the real values,
   run `bin/lan-validate.sh`. Put the fixed-lease list (one `dhcp-host=<mac>,<ip>[,<name>]` per line) in the file named by
   `AISIS_LAN_DHCP_HOSTS_FILE`. Seed the relay config (`config.json`, `voice-nginx.conf`) and the per-list domain files on
   the front host with `network/prepare_local.py`; they are the owner's inventory and are never overwritten.
2. **Static address for the front.** A DHCP server host cannot renew a lease from itself:
   `sudo bash bin/front-static-ip.sh --iface <iface> --addr <ip>/<prefix> --gateway <ip> --dns "<ip> <ip>"` on the front
   (`DRY_RUN=1` first). It pins the same address, tests connectivity and reverts by itself within 15 s on failure.
3. **Install.** `bin/front-install.sh` syncs files to the front, renders `front.env` and the DHCP config (mode 600),
   validates the compose project and shows a dry-run of `up`; `bin/front-install.sh --apply` then (re)creates the relay and
   the TLS front. It never starts DHCP or the watcher.
4. **DHCP cutover.** Never run two DHCP servers: stop the old one (router DHCP off, old host's server stopped), then
   `<front dir>/dc --profile dhcp up -d dhcp` on the front; check `dc --profile dhcp logs dhcp`. The container restarts by
   itself (`restart: unless-stopped`) and is **unguarded**: nothing re-checks that the router's DHCP stays off, so keep
   that check in your own monitoring on a host that is always on. Roll back by stopping the container and starting the
   old server (never both).
5. **Speaker resolver**: `speaker/speaker-dns.sh plan`, then `apply` (backs up the pinned resolver file, writes one
   `nameserver <resolver>` line, asks dhcpcd to regenerate, verifies, reverts by itself on failure); `undo` restores.
6. **Stop the old copies on the workstation — in this order.** First run
   `failover/install-failover-exclude.sh` so the watchdog no longer restarts them (stopping them first is pointless:
   the watchdog brings them back within seconds), prove the new path with `bin/verify.sh` (the speaker's lines appear
   in the front's TLS log and none in the old copy's), and only then stop the old relay/TLS copies for good.

Requirements on the front host: docker with Compose v2.24.4 or newer (the override uses `!reset`/`!override`, which
need 2.24.4; `front-install.sh` runs `docker compose config` there and fails if it is older), `rsync` (rsync over ssh
runs `rsync --server` on the front too, so it must be installed on BOTH the workstation and the front host),
key-based non-interactive ssh.

## Failover exclusion

The workstation's `aisis-failover.service` watches the relay and TLS containers and restarts them within seconds of a stop.
When they run on the front instead, `failover/install-failover-exclude.sh` installs a launcher into `$AISIS_CONFIG_DIR` and a
user-systemd drop-in that keeps every original argument of the unit's `ExecStart` and adds
`--exclude-component tls-voice --exclude-component dns-relay` (override with repeated `--component NAME`). The launcher filters
the deployed watchdog's component list before it starts, so nothing is edited in the deployed code. The launcher is
installed mode 700, so the unit can also run it directly through its shebang (no 203/EXEC), and after the restart the
installer waits a second and requires `systemctl --user is-active` — a watchdog that dies straight away is reported,
not silently assumed running. An unknown NAME exits 2 AT EVERY START, failing the whole unit on purpose: a typo or a
renamed component must never silently watch less (the installer validates the names against the deployed watchdog
before touching the unit, and `--dry-run` shows the rendered drop-in). The journal shows `failover-exclude:
watching ...; excluded ...` while it runs. `--dry-run` previews, `--uninstall` removes the drop-in and the launcher
and restarts the watchdog unfiltered (a working way out at any time). A tracked `--exclude-component` option in the
watchdog itself would replace this launcher.

## OPTIONAL fail-open watcher (read before using)

`watcher/backend_watcher.py` (compose profile `watcher`, off by default) flips only `enabled` in the relay config when the
backend's TCP ports stop answering (3 failures) and back (3 successes), so the relay forwards plainly instead of overriding
names while the backend is down. An `enabled=false` written by anyone else — `router_control.py forwarding --apply`, a
hand edit, a seeded config — is a MANUAL HOLD: the watcher adopts it and never switches the override back on by itself
(not even under `mode=forwarding`, where it would otherwise target the primary `target`); only a DOWN the watcher wrote
itself is automatically undone. **Do not use it for a speaker that is locked to the LAN by its own firewall:** with the
override off such a speaker cannot reach anything and loses its connection entirely. For such speakers keep the override on
and accept that voice is down while the backend is. The watcher mounts the relay config directory read-write; the relay mounts
it read-only.

## Capture kit

`capture/capture-both.sh --seconds 180` runs a time-boxed passive tcpdump on the workstation and on the front and prints a
summary: every DHCP message with server identifier and option 6, and the speaker's DNS queries by destination server. Use it
only to investigate (which DHCP server answers the client? which resolver does it query?); it is not part of the setup.

## Tests

Each file is independent and rootless: `tests/test-config.sh` (validator, DHCP generator), `tests/test-front-install.sh`
(fake front host), `tests/test-speaker-dns.sh` (fake device), `failover/test-failover-exclude.sh`,
`watcher/test_backend_watcher.py` (`python3 -m unittest`; the relay tests also need `TEST_UPSTREAM_IP`, a non-loopback
address of the machine), `capture/summarize.py --selftest`.

## Known limitations (LAN deploy)

- **The voice WebSocket crosses the LAN unencrypted.** TLS ends on the front (`tls-voice`); from there the speaker's
  audio (and its session headers) travel as `http://BACKEND:31115` in plaintext to the backend host, and the backend's
  port 31115 must accept connections from the LAN, which bypasses nginx's `allow`/`deny` list. Firewall port 31115 on
  the backend to the front's address only. Follow-up: put the front↔backend leg into an encrypted tunnel (WireGuard
  or an ssh tunnel) instead of opening 31115 to the LAN.

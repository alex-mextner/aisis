# AISIS Yandex backend

Specification: [150](../../docs/specs/150-yandex-chatgpt-backend.md).

The Python process runs as the desktop user so it can reuse the existing signed
in `pm chatgpt` setup. Quasar runs in Docker with host networking and reaches the
adapter on loopback. PostgreSQL alone publishes a loopback port. The adapter
must never bind publicly. Quasar's speaker protocol port is the only intended
LAN listener.

## Install and launch

Run `prepare-upstream.sh` once to populate the pinned build cache. Copy this
folder to `~/.local/share/aisis-yandex`, create a Python venv there and install
`requirements.txt`. Install ffmpeg if the existing voice environment does not
already provide it. Install `aisis-yandex-adapter.service` to
`~/.config/systemd/user/`. Copy `runtime.env.example` to the private path
`~/.config/aisis-yandex/runtime.env`, mode0600, and fill values from the existing
local credentials. Generate PostgreSQL and GLAGOL keys once and preserve them.
The GLAGOL key must remain stable across restarts.

`AISIS_CHATGPT_COMMAND` is a JSON argument vector, not a shell command. Its
executable reads a single JSON line and writes a single JSON response as
specified in150. `chatgpt_pm.py` calls the local AISIS pm browser service;
there is deliberately no invented `pm ask` operation.

```sh
systemctl --user daemon-reload
systemctl --user enable --now aisis-yandex-adapter.service
docker compose --env-file ~/.config/aisis-yandex/runtime.env -f compose.yaml up -d --build
curl --fail http://127.0.0.1:31117/healthz
curl --fail http://127.0.0.1:31116/devices
```

If an old long-lived terminal lacks the user's already-assigned Docker group,
use a fresh authenticated local login; do not change socket permissions or
account groups. Never print `docker compose config` with the real environment,
because the expanded output includes credentials.

`/healthz` reports configuration availability, not successful browser login or
a completed speaker migration. Run an actual turn to verify those components.
The adapter emits failure classes and Fish HTTP status without user text or
credentials. Upstream Quasar logs can contain transcripts; restrict access and
retention of its Docker logs.

## Rollback

Stop the core user services and this Compose project when rolling back the core
backend. Preserve the PostgreSQL volume, private keys and conversation state.
The core installation scripts do not change router or speaker configuration.

If the optional [network operator](network/README.md) changed DHCP DNS, follow
its captured router snapshot and guarded rollback procedure first. Keep its
forwarding resolver available for the retained DHCP lease period after restoring
the router, so clients with an older lease still resolve ordinary domains.
Stopping a resolver immediately after restoring router settings is insufficient.
Restore a speaker's recorded endpoint/configuration only if that speaker was
actually changed; also revert any temporary ADB settings if used.

## Speaker HTTPS front end

`prepare-proxy.sh` checks out `v3rt3p/https-proxy` at commit
`3b902d61b85642770cd96ed76ffaf80832c99289`. Its optional Compose profile
`speaker-tls` is separate from the speech backend and defaults to loopback ports
80 and443. Set private `AISIS_PROXY_SOURCE` and `AISIS_SPEAKER_BIND_IP` only after
checking port ownership and one speaker's supported firmware and rollback.
This proxy's certificates depend on the eMudhra trust weakness described by the
upstream project; a speaker with updated trust may reject them. A ready TLS
proxy is not evidence that a speaker accepts those certificates.

The upstream target is explicitly port31115. The default companion proxy's
port31120 is not the Quasar server's default and would route to the wrong place.
With this Compose host-network topology the proxy reaches the host through
`host.docker.internal`; the Quasar processor/TTS remain strictly loopback.

## Optional compatible streaming recognition

Reuse existing host speech tools and model caches first. If they cannot provide
Quasar's streaming WebSocket protocol, `prepare-stt.sh` checks out
`v3rt3p/quasistreaming-gigam` at
`e1770f19d67fe31908c5649cc680ad8ade77ed72`. The optional Compose profile
`streaming-stt` builds its CPU image, listens only on127.0.0.1:31121 and persists
the GigaAM-v3 checkpoint in a dedicated volume. First startup downloads a large
model; service startup is not ready until model initialization completes. The
transport accepts mono16kHz PCM16 and sample-rate metadata, and returns JSON
`text` with `end_of_utt`. Verify with an actual speech fixture. This profile does
not reserve or change the host GPU.

### Reuse existing Wyoming ASR

`stt_wyoming.py` provides Quasar's streaming WebSocket protocol over an existing
Wyoming recognizer, including WebRTC voice activity detection,200ms preroll,
800ms end silence,30s utterance cap, and a bounded40s idle lifetime. It accepts
mono PCM16 at8/16/32/48kHz with the mandatory initial `sample_rate` JSON message.
Audio is kept in RAM; the gateway does not download or load a recognition model.
The existing ASR receives `transcribe`, `audio-start`, audio chunks and
`audio-stop`, and its final `transcript` becomes Quasar's `end_of_utt` response.

Set the private `AISIS_WYOMING_URI` and enable `aisis-yandex-stt.service`. If the
recognizer is reachable only from its own host's Docker network, the optional
`aisis-yandex-wyoming-tunnel.service` uses existing SSH authentication and host
verification to forward localhost31122 to the verified container endpoint.
Set `AISIS_WYOMING_REMOTE_HOST`, `AISIS_WYOMING_REMOTE_PORT` and
`AISIS_WYOMING_SSH_TARGET` privately. No Home Assistant network/container setting
is changed. A container IP can change after recreation; re-check it if the
recognizer becomes unavailable.


## Existing pm browser integration

`pm_cdp_server.py` attaches to the already running, account-verified pm Chrome
CDP endpoint. It does not launch Chrome, copy profiles, obtain browser tokens,
switch model or effort, enroll recovery, clear Guardian controls, or close
existing tabs. It uses the installed `agenttools_chatgpt` implementation's
account-scoped Store and protected dispatch transaction. Existing manual stops,
quota blocks and uncertain sends continue to block the operation.

The service binds to127.0.0.1:31118; authenticated `POST /turn` receives exactly
`station_id`, `text`, `turn_id`, and optional `conversation_id`. It returns
`text` and the permanent conversation URL as `conversation_id`. The private
`AISIS_ALLOWED_STATIONS` list is required. Every speaker has an owned tab and
its own durable conversation pointer. The existing browser's tab capacity is
respected. Across a browser restart, an exact saved permanent conversation can be
reattached or reopened in a new owned tab. Duplicate matches and a live owned
tab that was manually navigated are refused for inspection.

Install `aisis-yandex-pm.service` and configure `AISIS_PM_CDP`,
`AISIS_PM_WORKSPACE`, `AISIS_PM_GUARDIAN_STATE`, `AISIS_PM_STATE_DB` and a random
`AISIS_PM_TOKEN` in the private environment. The unit uses the already installed
Guardian Python environment; the adapter client uses its own small venv. Start
the pm service before the adapter. `GET /health` reports service availability,
not a proven successful send or login.

Browser mutation lives in the persistent pm service. Canceling the adapter's
HTTP client cannot cancel a JavaScript operation that Chrome may already be
executing. Shutdown drains pending guarded work; its unit deliberately has no
forced stop timeout. A failed or interrupted dispatch is recorded privately and
never resent. Inspect the owned chat and ledger before reconciling an uncertain
turn. Do not delete state or clear a Guardian stop to make a request succeed.

For an already deployed legacy HA ChatGPT-web service, the optional client path
`AISIS_CHATGPT_WEB_URL` can use its OpenAI-compatible route and local history
prefix matching instead. This route is accepted only on a loopback HTTP service
and never points to the paid OpenAI API.


## Native speech completion and cancellation

The supported Wyoming gateway distinguishes normal native audio EOF from
cancellation. Quasar's patched `VoiceInputHandler` flushes queued audio and calls
its STT session's `finish()`. The WebSocket client sends `{"type":"finish"}`,
keeps the connection open, and waits up to40s for the final `end_of_utt` result.
The gateway flushes speech without needing extra silence and permits up to35s
for recognition. Native EOF arriving during an existing VAD-triggered ASR call
joins that same call. A shared finalization promise emits exactly one finish
when VAD and native Close race.

Cancellation closes the STT transport immediately (the gateway also accepts
`{"type":"cancel"}`). Session identity/epoch guards suppress callbacks and
finish events from a cancelled or replaced input. A timeout emits an error;
it never substitutes the last partial or an empty successful transcription.
One Quasar STT WebSocket carries one utterance.

`test-quasar-native.sh` runs the actual patched TypeScript handler and WebSocket
client in the built Quasar image with network access disabled. Its optional
`--live` mode uses an explicitly supplied synthetic Брачо Ogg fixture and the
running local Wyoming gateway, proving Opus decoding→native EOF→final ASR. It
does not exercise a physical speaker or send a ChatGPT prompt.

The optional GigaAM build profile is unused scaffolding. Its upstream server
must first implement and verify the explicit EOF control before that profile
can replace the supported Wyoming gateway.

## Optional source-scoped speaker routing

The [network add-on](network/README.md) provides a separately reviewed
source-scoped DNS/HTTPS path and router backup/rollback tooling. Its voice-only
profile routes only `uniproxy.alice.yandex.net` to this backend; Quasar account,
sync and `scbh` responses continue to the original service. The UniProxy voice
connection has no dependency on this backend's OAuth/account or GLAGOL state,
so this mode does not require ADB bootstrap or replacement account settings.

Firmware acceptance of the proxy certificate, actual LAN routing and a
microphone-to-speaker result must be established on the device. Source review,
a running proxy and synthetic tests do not establish hardware compatibility.

## Ogg packet boundaries before Opus decoding

The pinned upstream decoder treated each Ogg lacing segment as one Opus packet.
A real Брачо fixture exposed valid packets larger than255bytes, which that
implementation attempted to decode before receiving the rest of the packet.
The reviewed codec patch assembles complete packets through255-byte lacing
chains, including continuation across pages and the required zero terminator
for a packet whose size is an exact multiple of255. It tracks page sequence,
stream serial and continuation flags, bounds pending packet memory, and rejects
truncated page headers/tables/bodies before decoding shortened data.

Identification/comment headers are handled by completed packet boundaries.
Input sample rate in OpusHead is informational; decoding uses48kHz PCM supported
by the gateway, including valid input metadata of0 or44100. Signed output gain
and channel mapping use their specified header offsets. The output gain is
applied by the actual Opus decoder.

The actual-encoder/decoder regressions construct valid Ogg pages with CRC,
exercise packet/page boundaries and signed gain, and run alongside native EOF
regressions. See [Ogg framing](https://xiph.org/ogg/doc/framing.html) and
[Ogg Opus RFC7845](https://www.rfc-editor.org/rfc/rfc7845).

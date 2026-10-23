# 150 — Yandex speakers with ChatGPT web and Брачо

Status: implementation in progress. This deployment specification follows the
owner's explicit request to use the existing `pm chatgpt` browser session, rather
than the older OpenClaw gateway plan.

## Outcome

Run the replacement Quasar speech backend on the existing Ubuntu host. A known
speaker sends speech to local streaming recognition, its transcript to ChatGPT
web, and hears the resulting answer in the existing Брачо voice. Each speaker
keeps its own ChatGPT conversation across dialogs and service restarts. Other
browser tabs and the existing ChatGPT guardian remain available.

## Components and interfaces

* Quasar is built from `v3rt3p/quasar-server` commit
  `a983cda873f80be66d820bffe01e09f125f62cf8` (GPL-3.0 upstream).
  Keep upstream code in a private build cache, not copied into this repository.
* A small checked patch forwards `x-uprx-device-id` as processor metadata
  `stationId`. Missing speaker identity fails closed. Device ADB is disabled by
  default; upstream public DNS forwarders and unbound are disabled by default.
* PostgreSQL stores Quasar registration and speaker configuration. It listens
  only on loopback port 31119 and persists in a dedicated Docker volume.
* Quasar serves speaker protocol on port 31115. Its unauthenticated admin API
  binds to loopback port 31116 in application code.
* The host Python adapter binds to loopback port 31117. `/processor` implements
  the Quasar JSON WebSocket protocol; `/v1/audio/speech` implements the necessary
  OpenAI speech operation and returns real Ogg Opus.
* The adapter invokes one browser command per accepted speech turn. JSON stdin
  has `station_id`, `text`, `conversation_id`, `turn_id`; JSON stdout has `text`
  and `conversation_id`. No shell interpolation, browser API guess, replay, or
  retry is permitted. Timeouts and disconnections terminate its process group.
* Voice uses the existing Fish Брачо reference
  `5cd1ffa5dbb4419cb5766435139c6d92`, model `s2.1-pro-free`; credentials stay in
  the existing private configuration or a mode-0600 runtime environment.
* Streaming STT must match Quasar's sample-rate JSON + PCM binary WebSocket
  protocol and return `{text,end_of_utt}`. Reuse available host recognition
  dependencies before downloading another model.

## Reliability and privacy

Per-speaker locks prevent simultaneous turns from corrupting one conversation.
SQLite stores conversation pointers and turn status, never speech transcripts.
An interrupted turn is marked interrupted after restart and is never resent.
A browser authorization failure produces an explicit spoken failure. External
unsolicited events are disabled until separately requested. Runtime identifiers,
network inventory, tokens, certificates, cookies, and environment files must
never enter this public repository.

## Acceptance and rollout

1. Verify the processor protocol with two isolated simulated speaker identities,
   failed authentication, missing identity, timeout, and cancellation.
2. Verify a real ChatGPT turn through the existing browser mechanism and a
   follow-up in the same conversation; check another speaker remains separate.
3. Verify Брачо synthesis and inspect encoded Opus audio, not just HTTP status.
4. Verify Quasar and PostgreSQL start, data persists, and admin listeners bind
   only to loopback.
5. Record each speaker's exact platform, firmware, current endpoint/config and
   recoverable backup privately before changing that speaker.
6. Migrate one supported speaker, prove microphone→STT→ChatGPT→Брачо→speaker,
   then migrate further supported devices. No household-wide router/DNS change
   is part of deployment until device compatibility and rollback are known.
7. Report backend readiness separately from actual speaker migration. Neither a
   running container nor a successful simulated turn is an end-to-end speaker
   pass.

The Mini 2 bootstrap article is research, not proof that every Yandex model can
be migrated. Restore original endpoint/config on rollback; any temporary ADB
setting must also be reverted explicitly.


## Native stream EOF

Normal native `StreamControl.Close` is an explicit finish, rather than a
cancel. The patched STT session sends the gateway a finish control and waits
for its bounded final result. Native EOF flushes even when VAD has not observed
trailing silence, and joins an already running recognition. VAD and native EOF
share exactly one finalization. Cancel/replacement invalidates the old session
before closing it; late results cannot emit another turn. Actual patched
TypeScript handler tests cover these boundaries, including delayed recognition.

## Optional narrow network deployment

See [network operation and rollback](../../backend/yandex/network/README.md).
Prefer routing only the UniProxy voice WebSocket domain to AISIS after the TLS
compatibility gate. Leave original account/sync/GLAGOL endpoints untouched.
The reviewed UniProxy constructor and input lifecycle depend on the speech
backends and station identity, without account storage or OAuth initialization.
This is a source-level routing design, not a hardware compatibility claim.
Router rollback restores the captured values and retains the forwarding DNS
service until clients with older DHCP leases have aged out.


## Ogg/Opus framing integrity

The patched codec reassembles Ogg lacing segments into complete Opus packets,
including packets larger than255bytes, exact-multiple zero termination and
cross-page continuations. It rejects page sequence/serial discontinuities and
truncated page framing before decoding. Opus headers are interpreted on packet
boundaries; the decoder emits48kHz PCM and honors signed header output gain.
Real encoder/decoder tests and the unchanged synthetic Брачо fixture verify this
boundary separately from physical speaker compatibility.

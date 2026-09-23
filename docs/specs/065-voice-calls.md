# Central Voice Calls

## Goal

Move generic call ownership out of HyperCalendarBot into the central assistant.

Calendar remains a caller/client: it can request “call this user with this reminder at this time,” but it does not own transports, TTS/STT, live sessions, call queues, or call diagnostics.

## Transports

### PSTN

Reuse OpenClaw's official voice-call plugin for Twilio/Telnyx/Plivo rather than implementing PSTN from scratch.

### Telegram P2P

Migrate the existing HyperCalendarBot Telegram P2P transport behind an AISIS call transport contract.

Existing assets include:
- Python Pyrogram/pytgcalls bridge;
- patched ntgcalls network-availability fix;
- Bun call manager and BullMQ queue;
- bidirectional call session;
- RU/EN streaming STT;
- TTS and interruption handling.

The transport can remain a separately deployed service initially while calendar call ownership is removed.

## Core call contract

The central service exposes operations equivalent to:
- `start_notification_call(recipient, speech, options)`;
- `start_conversation_call(recipient, opener, session)`;
- `get_call(call_id)`;
- `cancel_call(call_id)`;
- provider/transport health and smoke tests.

Calls produce structured lifecycle events: queued, ringing, connected, listening, thinking, speaking, completed, failed.

## Reliability requirements

A call transport is not “healthy” because a process started.

Health proof includes:
- dependency/config check;
- auth/session check;
- TTS synthesis check;
- STT connectivity check when conversational mode is enabled;
- media conversion check;
- transport handshake;
- test-account end-to-end call with non-silent received audio;
- timeout/hangup behavior;
- repeat/replay/idempotency behavior.

For Telegram P2P, the historical silent-audio failure caused by ntgcalls missing network availability must have a regression/diagnostic check so deployment cannot silently revert to the broken binary.

## Migration

1. define transport-neutral call contracts in AISIS;
2. add AISIS call client API;
3. move/extract Telegram call implementation and tests from HyperCalendarBot;
4. change calendar reminders to call the AISIS API;
5. remove generic call code from calendar once production parity is proven.

No destructive migration occurs before real end-to-end parity.

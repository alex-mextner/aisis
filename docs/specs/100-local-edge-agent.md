# Local Edge Agent / Binder

## Goal

Provide a one-click bridge between AISIS and private/local capabilities on the user's computer without opening inbound ports.

It enables local Codex, Claude Code, OMP, private Telegram MTProto sessions, Tailnet-only Home Assistant, and future local files/apps.

## Packaging

The edge agent is a standalone signed binary with installers for macOS and Windows.

It must not rely on a system Python installation. Python-based plugins, when needed, are managed inside the product rather than assumed from the OS.

## Pairing UX

The assistant can send a short-lived signed setup link.

The installer opens a browser pairing flow, shows the device name and requested capability groups, and requires explicit user approval.

After pairing, the edge agent maintains an outbound authenticated connection to AISIS.

## Executor discovery

The agent detects supported local executors such as `codex`, `claude`, and `omp`, reports capability/version metadata, and never uploads their credentials.

Executions are represented as durable AISIS jobs with logs/progress filtered for secrets.

## Private connectors

Home Assistant and Telegram MTProto can run as edge-owned connectors.

The cloud sees typed tool results; private session keys and HA long-lived tokens can remain on the device.

## Security

Pairing keys are device-scoped and revocable.

Commands are allowlisted by capability; arbitrary shell execution is not a default capability.

Local actions have the same risk/confirmation policy as cloud tools and produce auditable receipts.

## Offline behavior

The cloud marks a device unavailable when the outbound channel is down and can keep a job queued or choose an explicitly configured cloud fallback.

It never silently substitutes a different computer for a device-scoped action.

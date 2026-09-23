# Local Edge / Desktop Transport

## V1 decision

Reuse the public `alex-mextner/open-remote-commander` repository as the first AISIS desktop/edge transport.

Repository: https://github.com/alex-mextner/open-remote-commander

AISIS should not block its first useful version on a second desktop connector.

## Responsibilities

The edge transport exposes user-approved capabilities such as:
- filesystem and process access;
- local application/browser automation where supported;
- private network/Tailnet resources;
- installed agent harnesses (Codex, Claude Code, OMP);
- optional Telegram MTProto session ownership.

It maintains outbound authenticated connectivity and does not require opening arbitrary inbound ports.

## Harness selection

The desktop transport **advertises** available harnesses and versions.

The central AISIS/OpenClaw runtime chooses the harness/model/effort for each job. Local transport is execution plumbing, not routing policy.

## Open Desktop Commander bridge

Initial AISIS integration may use open-remote-commander's existing remote API/MCP capabilities to start and observe local harness processes.

The bridge must expose typed execution handles, cancellation, progress/log events, workspace selection, and capability discovery rather than a raw unrestricted shell as the only interface.

## Go packaging target

Open Desktop Commander / the AISIS edge component should migrate toward a signed standalone Go binary for macOS and Windows so installation does not depend on npx, Node, or Python.

A chat/web setup link can download the correct installer, pair the device, and return to AISIS.

## Security

Device keys are revocable and capability-scoped. Local credentials stay local when possible.

Commands/actions remain subject to central policy and audit.

The system never silently substitutes a different device for a device-scoped task.

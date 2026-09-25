# Local Edge / Desktop Transport

## V1 decision

Reuse **Open Remote Commander (ORC)**, the public `alex-mextner/open-remote-commander` repository as the first AISIS desktop/edge transport.

Repository: https://github.com/alex-mextner/open-remote-commander

Open Remote Commander is **already a Go implementation** with a Go MCP gateway/relay, outbound Go device agent, pairing, device control, process/filesystem tools, and OAuth-introspection support. AISIS must extend it rather than start another binder.

## Responsibilities

The edge transport exposes user-approved capabilities such as:

- filesystem and process access;
- local application/browser automation where supported;
- private network/Tailnet resources;
- installed agent harnesses (Codex, Claude Code, OMP);
- optional Telegram MTProto session ownership.

It maintains outbound authenticated connectivity and does not require arbitrary inbound ports on the user's computer.

## Harness selection

The desktop transport **advertises** installed harnesses, versions, supported capabilities, workspace roots, and current availability.

The central AISIS/OpenClaw runtime chooses the harness/model/effort. ORC is execution/transport plumbing, not the routing policy engine.

## Harness execution extension

Add a first-class ORC capability above generic `start_process`:

- discover harnesses;
- start a harness job with typed instruction/workspace/options;
- return a stable execution id;
- stream/read progress and bounded logs;
- accept user answers/continuations;
- cancel;
- report final status/artifacts;
- redact configured secret patterns.

The generic process tools remain available for debugging but should not be the production harness contract.

Harness jobs are default-deny outside an explicit opaque workspace binding. The central request includes principal, device, harness, and workspace identity; ORC resolves that workspace locally to an allowlisted canonical path. Model-provided text never becomes an unrestricted raw-shell command merely because a harness job was requested.

## Packaging work

Do **not** rewrite ORC in Go: it is already Go.

The remaining distribution target is signed, low-friction installers and auto-update metadata for macOS and Windows, plus persistent service integration where appropriate. Installation must not require npx, Node, or Python.

A chat/web setup link can download the correct installer, pair the device, and return to AISIS.

## Security

Device keys are revocable and capability-scoped. Local credentials stay local when possible.

Commands/actions remain subject to central policy and audit.

The system never silently substitutes a different device for a device-scoped task.

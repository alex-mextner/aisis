# Web Setup and Control Surface

## Purpose

The web UI is the low-friction place for account linking, provider keys, permissions, device pairing, model aliases, delivery settings, and debugging.

Chat surfaces can deep-link directly to the exact setup page the user needs.

## Authentication

The web app authenticates the AISIS principal and supports provider OAuth flows.

Short-lived setup links from Alice/Telegram preserve the originating principal/conversation without exposing secrets in the URL.

## Provider setup

Provider setup is a simpler UX over OpenClaw's provider/SecretRef configuration (spec 040), not a parallel provider stack: credentials are stored and used by OpenClaw.

Users can connect Hugging Face, OpenRouter, OpenAI, DeepSeek, and any other provider OpenClaw or an OpenClaw provider plugin supports.

The UI validates a credential through OpenClaw's provider check where one exists, shows available models, maps them to the semantic aliases (`fast`, `balanced`, `deep`, `background`) with fallback order, and shows spend/usage controls where available.

## Resource setup

The UI shows each linked resource and granular grants: read/search/write/send/delete/admin as applicable.

Home Assistant/MTProto can be marked “edge-owned”, making clear that credentials remain on the paired computer.

## Edge pairing

A “Connect this computer” flow selects macOS/Windows, downloads the signed Open Remote Commander (ORC) installer, and completes device pairing in the browser (spec 100).

The device page shows the harnesses ORC advertises and the private connectors individually, with explicit enable/disable controls. Generic process tools stay off unless the user grants them for that device (spec 100, Security).

## Jobs and debug

Users can inspect active/recent jobs, their route/model, progress, tool activity, result, and delivery state.

A trace view explains routing/tool failures without exposing private tokens or hidden model reasoning.

## Privacy defaults

New connectors begin with the minimum practical scope.

Write permissions and proactive Station delivery are opt-in rather than inferred from successful read authorization.

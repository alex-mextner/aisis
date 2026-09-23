# AISIS Master Spec

**Status:** approved architecture, implementation-ready
**Language:** English identifiers; Russian-first UX
**Authority:** this document defines product boundaries. Subsystem specs refine it but must not contradict it.

## 1. Product intent

AISIS is a continuously available personal AI assistant with explicit, permissioned access to a person's digital resources. It should feel like one assistant across Alice, Telegram, web, calls, and local computer harnesses.

The initial domains are calendar, finance, home, general questions, personal Telegram, email, Notion, and Slack. AISIS also supports long autonomous work, proactive monitoring, durable automations, self-learning, and visual workflow authoring.

## 2. Runtime decision

AISIS uses **OpenClaw as the primary central-agent runtime** rather than rebuilding session continuity, memory, model providers, harness integration, proactive heartbeat, background tasks, self-learning, channel routing, and automation from scratch.

AISIS is not a hard fork of OpenClaw by default. It is a public product repository containing OpenClaw plugins, domain adapters, shared libraries, configuration, tests, and git submodules for existing products.

Hermes Agent remains an evaluated alternative/reference implementation. Its learning loop, Python/uv implementation, Home Assistant integration, cron/skills, and messaging gateway are useful references, but V1 targets OpenClaw because its current runtime already includes channel plugins, Codex/Claude runtimes, self-learning, durable automation/task-flow primitives, and typed resumable Lobster workflows.

AISIS-specific domain APIs remain runtime-neutral enough that a future Hermes adapter is possible without forcing a lowest-common-denominator runtime abstraction into V1.

## 3. Existing systems become first-class subrepositories

HyperCalendarBot remains the calendar source of truth. It is included in the AISIS checkout as a git submodule and gains a stable authenticated API/tool surface. Telegram remains supported by HyperCalendarBot, while Alice and the AISIS aggregator use the same calendar logic through that API.

ExpenseSyncBot remains the finance source of truth and is included as a git submodule. It gains a stable API/tool surface.

VibeFlow remains its own repository and is included as a git submodule for the visual workflow editor/runtime. AISIS adds a shared Workflow IR and compilation/integration layer rather than replacing VibeFlow.

Open Remote Commander (ORC, `alex-mextner/open-remote-commander`) remains its own public Go repository and is included as an integration submodule. It is the initial desktop/edge transport for private resources and local harness execution.

`dext0r/yandex_smart_home` remains responsible for native Alice Smart Home commands such as “включи свет”. AISIS does not duplicate that path.

## 4. High-level architecture

```mermaid
flowchart LR
  A[Alice channel plugin] --> OC[OpenClaw Gateway / main agent]
  T[Telegram] --> OC
  W[Web / Control UI] --> OC
  C[Voice calls] --> OC

  OC --> MEM[Memory + Self-learning]
  OC --> AUTO[Automations + Heartbeat + Tasks]
  OC --> ROUTE[Routing: deterministic / Laya / Jev]
  OC --> TOOLS[AISIS tools/plugins]

  TOOLS --> HC[HyperCalendarBot API]
  TOOLS --> EF[ExpenseSyncBot API]
  TOOLS --> HA[Home Assistant]
  TOOLS --> PC[Telegram MTProto / Email / Notion / Slack]
  TOOLS --> CALC[@aisis/calculator]

  VF[VibeFlow visual editor] --> IR[AISIS Workflow IR]
  IR --> LOB[Lobster / Task Flow]
  IR --> VFR[VibeFlow native runtime]

  OC --> EDGE[Open Remote Commander (ORC)]
  EDGE --> HARNESS[Codex / Claude Code / OMP]
  EDGE --> LOCAL[Local & Tailnet resources]
```

## 5. Deployment and tenancy

Development starts in **single-principal mode**: one trusted person, one OpenClaw workspace/runtime, multiple personal surfaces.

A public hosted Alice skill is a different trust model. Before catalog publication for unrelated users, AISIS must map each principal to an isolated OpenClaw workspace/runtime and isolated secrets. OpenClaw's convenient main session may be shared across one principal's channels, but never across unrelated principals.

OpenClaw multi-agent/workspace routing is useful orchestration, not by itself the security boundary for hostile multi-tenancy. Hosted mode must use process/container/storage isolation appropriate to the deployment.

## 6. OpenClaw responsibilities

OpenClaw owns the general agent loop, main-session continuity, model/provider integration, tool policy, agent harness routing, built-in memory, Skill Workshop/self-learning, automations, heartbeat, background tasks, Task Flow, delivery to supported channels, and generic approval primitives.

AISIS should extend these via public plugin/channel/tool interfaces before modifying OpenClaw core. Upstream patches are a last resort and should be small enough to upstream.

## 7. AISIS responsibilities

AISIS owns:
- Yandex Alice channel plugin and account-linking UX;
- principal/resource linking across personal services;
- domain APIs and adapters for calendar and finance;
- Home Assistant conversational tools beyond native Yandex Smart Home;
- reusable calculator/rendering library;
- Telegram personal MTProto connector and fuzzy recipient resolver;
- central voice/call capability, including Telegram P2P call transport;
- VibeFlow integration and Workflow IR;
- model-routing policy additions such as Laya/Jev;
- setup UX, opinionated defaults, product tests, and cross-domain policies.

## 8. Proactivity and learning

AISIS uses OpenClaw automations for explicit one-shot/recurring work, heartbeat for ambient awareness, background tasks for detached work, and standing instructions/skills for persistent behavior.

Self-learning is exposed through OpenClaw Skill Workshop. The initial AISIS default is conservative: learned procedures that can cause external writes are proposed for review before activation; users may enable autonomous maintenance for trusted skill classes.

Proactive actions still respect resource ACLs, quiet hours, action risk classes, deduplication, and notification policy.

## 9. Conversation and long work UX

Fast deterministic/tool requests should complete synchronously when possible.

Long work becomes a durable OpenClaw background task/Task Flow. The assistant immediately acknowledges it and supports natural status queries.

Telegram may receive proactive progress/final delivery.

A standard Alice Dialogs webhook cannot stream a later continuation after the 4.5-second response deadline. AISIS therefore acknowledges and continues as background work.

If a long task **originated from Alice**, proactive delivery through the same configured Station is allowed and may speak the full result when the user has enabled this behavior. It is not restricted to a generic “ready” notification merely because the result is private. If the originating Station cannot be resolved or proactive speech is disabled, the result remains pending and can be requested on the next Alice turn.

## 10. Alice

Alice is implemented as an OpenClaw channel plugin with a public HTTPS webhook, channel/session binding, pairing/account-link support, outbound structured response rendering, and display/TTS separation.

`display_text` and `speech_text` remain separate first-class representations.

Alice voice recognition is not used as a security boundary. Cross-person calendar access is based on explicit delegation/ACL.

## 11. Calendar

HyperCalendarBot is modified to expose stable domain APIs; it is not merely wrapped without changes.

The same calendar engine handles Telegram and AISIS requests. The API must support agenda lookup, search, create/edit/delete, free/busy, invitations/sharing, reminders, Google account status, and surface-neutral intent execution.

## 12. Finance and calculator

ExpenseSyncBot is modified to expose stable finance APIs.

Generic calculator semantics are extracted into a reusable monorepo library `@aisis/calculator`. ExpenseSyncBot consumes that library rather than remaining the owner of generic arithmetic.

The rendering layer produces canonical value, display text, and speech text independently. Exact/simple rational results such as one third may display as `1/3` while speaking “одна треть”.

## 13. Home Assistant

Native Alice smart-home exposure remains with `dext0r/yandex_smart_home`.

AISIS adds AI-assisted Home Assistant read/action tools for compound/contextual requests.

Home Assistant endpoints are runtime deployment configuration and are not committed to the public repository.

AISIS supports both private Tailnet/local access and an authenticated HTTPS reverse-proxy profile. Private/local access remains preferable for high-trust operations when available.

## 14. Calls

Calls are a central AISIS capability, not a calendar subsystem.

The existing HyperCalendarBot Telegram P2P call stack is extracted/migrated behind a central call transport API. Calendar reminders become clients of the call service.

OpenClaw's official voice-call plugin is reused for PSTN providers and its session/realtime/security patterns are reused for the central abstraction. Telegram P2P remains a separate transport because it uses MTProto/WebRTC rather than Twilio/Telnyx/Plivo.

Call reliability requires automated setup diagnostics, transport smoke tests, state-machine tests, STT/TTS tests, and real end-to-end test-account calls before being declared healthy.

## 15. Workflows / no-code

Neither Hermes nor OpenClaw currently provides an n8n-style visual graph editor. OpenClaw provides strong execution primitives: Automations, Task Flow, Lobster typed workflows with approval/resume, hooks, and LLM Task.

VibeFlow becomes the visual/no-code programming surface for AISIS.

AISIS defines a typed Workflow IR that VibeFlow can emit. Initial compilation/execution targets are:
1. OpenClaw Lobster for deterministic typed pipelines with approval/resume;
2. OpenClaw Task Flow/Automations for durable/background/scheduled execution;
3. VibeFlow native executor for n8n-compatible nodes and graph features not representable in Lobster.

The visual editor must show which target a workflow can compile to and why.

## 16. Models and routing

OpenClaw provider/harness support is reused rather than duplicating a model gateway.

AISIS adds an opinionated routing layer:
- deterministic routing first;
- optional local multilingual Laya;
- optional Jev decision model;
- static fallback.

Routing chooses model/provider/harness and reasoning effort separately.

Fast defaults may use DeepSeek V4.1 Flash. Hard tasks may route to configured frontier models or local/remote harnesses such as Codex, Claude Code, or OMP.

## 17. Local computer access

V1 reuses the user's public Open Remote Commander (ORC) as the first edge/desktop transport for local files, terminals, and harness access.

The long-term packaging target is a signed Go binary for macOS and Windows, avoiding Node/npx/Python prerequisites.

Harness selection is a central-server routing decision. The edge transport advertises available harnesses/capabilities; it does not decide which harness should receive a job.

## 18. Personal connectors

V1 optional connectors include Telegram personal account (MTProto), email, Notion, and Slack.

Telegram recipient resolution uses stable numeric peer identity; username/display name are hints. Ranking combines aliases, normalization, transliteration, fuzzy matching, recency, frequency, reply behavior, mutual-chat context, and prior confirmed resolutions.

## 19. Repository and openness

AISIS is public/open source.

The top-level repository includes HyperCalendarBot, ExpenseSyncBot, VibeFlow, and Open Remote Commander as git submodules so each project can still be developed, released, and deployed independently.

Secrets, local endpoints beyond intentionally documented examples, sessions, and user data are never committed.

## 20. Acceptance criteria

A user can contact the same central assistant from Telegram and Alice and retain coherent identity/session behavior.

Calendar and finance behavior comes from their existing services through authenticated APIs.

The calculator is a reusable shared package.

A long task can outlive the initiating request, report status, and deliver later.

The assistant can learn reusable procedures, perform scheduled/proactive work, and expose those behaviors for review/control.

A VibeFlow graph can compile to at least one durable AISIS/OpenClaw workflow target.

Calendar reminders can request calls without importing calendar-owned call implementation.

Home Assistant compound requests work without replacing the existing Yandex Smart Home integration.

Local Codex/Claude Code/OMP execution can be reached through an edge transport while harness choice remains central.

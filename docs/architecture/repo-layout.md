# Repository Layout

The initial implementation plan should preserve these boundaries while allowing folders to change if tooling requires it.

```text
aisis/
  apps/
    api/                 # FastAPI ingress: Alice, OAuth callbacks, public API
    worker/              # durable background jobs
    telegram/            # universal Telegram assistant surface
    web/                 # setup/settings UI
    edge/                # standalone local binder (Go)
  packages/
    core/                # Turn/Answer/conversation orchestration
    identity/            # principals, external identities, grants
    routing/             # deterministic + Laya/Jev route decisions
    models/              # BYOK provider adapters
    tools/               # tool registry/contracts
    jobs/                # durable job state machine
    delivery/            # Telegram/Alice inbox/Station adapters
    rendering/           # text, speech, rich output, numbers
    connectors/          # email/Notion/Slack interfaces
    telegram_personal/   # MTProto edge connector protocol
    home_assistant/      # HA semantic tools
    domain_adapters/
      hypercalendarbot/
      expensesyncbot/
  docs/
    specs/
    architecture/
    research/
```

## Language/tooling decision

The cloud/orchestration layer is typed async Python managed by `uv`.

The web UI may use TypeScript where a richer client is justified, but configuration APIs stay typed from one schema source.

The edge binder is a standalone Go binary to make macOS/Windows installation independent of a preinstalled Python runtime.

Existing HyperCalendarBot and ExpenseSyncBot remain TypeScript/Bun repositories and integrate over typed adapters.

## Deployment units

The API and worker are separate processes sharing durable storage/queue abstractions.

The Telegram surface may initially run with API or separately, but must not own domain state.

The edge agent is user/device software and is versioned/revocable independently.

Domain adapters may call existing services over authenticated internal HTTP/MCP or live in a thin compatibility package when colocated.

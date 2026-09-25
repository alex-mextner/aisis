# AISIS

AISIS is a universal AI assistant with explicit, permissioned access to a person's digital resources.

The product is not tied to one messenger, one model, or one automation platform. The same assistant can be reached from Alice, Telegram, the web UI, a local voice speaker, and local computer agents while reusing the same identity, conversations, tools, jobs, memory, and policy layer.

## Product shape

- **Surfaces:** Alice, Telegram, web/API, local edge agent, local voice speaker.
- **Existing domain services:** HyperCalendarBot for calendar, ExpenseSyncBot for finance/calculator behavior.
- **New domain service:** Home Assistant conversational agent, without replacing `dext0r/yandex_smart_home`.
- **Personal connectors:** Telegram MTProto, email, Notion, Slack.
- **Model providers:** BYOK cloud providers plus local Codex / Claude Code / OMP executors.
- **Long work:** durable jobs with status, progress, cross-channel delivery, and resumable results.

The authoritative product definition is [docs/specs/000-master-spec.md](docs/specs/000-master-spec.md).

## Documentation map

- `docs/specs/` — product and subsystem specifications.
- `docs/architecture/contracts.md` — typed contracts and discriminated unions.
- `docs/architecture/repo-layout.md` — repository boundaries and deployment shape.
- `docs/research/` — verified platform constraints and existing-system notes.

## Design principles

1. Fast deterministic paths before LLM calls.
2. Domain logic stays in domain services; the assistant orchestrates it.
3. Display text and speech are separate first-class outputs.
4. Long work is a durable job, never a long webhook request.
5. Secrets stay out of prompts; local private sessions stay local whenever possible.
6. Every external write has an explicit policy and audit trail.
7. Model routing is replaceable and provider-neutral.

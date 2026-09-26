# Model Routing and Providers

## Runtime ownership

OpenClaw owns provider clients, provider credentials, model sessions, CLI/harness runtimes, health/fallback mechanics that it already implements.

AISIS does **not** maintain a parallel Hugging Face/OpenRouter/OpenAI/DeepSeek client stack merely to normalize providers a second time.

When a provider is missing from OpenClaw, prefer an OpenClaw provider plugin. A direct AISIS provider adapter is a compatibility escape hatch, not the default architecture.

## User configuration

Users connect their own providers through OpenClaw/AISIS setup and map concrete model references to semantic aliases:

- `fast` — low-latency routing, extraction, small answers;
- `balanced` — normal conversational reasoning;
- `deep` — difficult reasoning;
- `background` — long-running throughput/cost-optimized work.

AISIS web/chat setup may provide a simpler UX over OpenClaw's underlying provider/SecretRef configuration.

## Route decision

AISIS adds an opinionated `RouteDecision` ahead of model execution. It may choose:

- deterministic/tool-only execution;
- OpenClaw model alias/reference;
- OpenClaw CLI/harness backend such as Codex or Claude;
- remote harness capability advertised through Open Remote Commander (ORC);
- reasoning effort;
- relevant tool groups;
- synchronous vs background execution.

The routing chain is:

1. deterministic intent/tool rules;
2. optional local multilingual Laya;
3. optional Jev decision model;
4. conservative static fallback.

For Russian routing, use the multilingual Laya checkpoint and preload it when local routing is enabled. Jev is optional; loss of Jev never blocks the static/deterministic fallback.

## Effort

Reasoning effort is a separate routing dimension from model identity. Canonical AISIS levels are `none | low | medium | high | xhigh | max`.

The OpenClaw provider/harness adapter maps a canonical effort to what the selected backend actually supports. Unsupported effort levels degrade explicitly rather than silently changing model class.

## Examples

A configured DeepSeek V4.1 Flash endpoint may back `fast`.

A configured frontier model may back `deep`.

A repository-analysis request may bypass ordinary chat models and route to Codex/Claude/OMP through an available local or remote harness.

These are configuration examples, not hard-coded product dependencies.

## Health and fallback

OpenClaw's provider/runtime health is the execution source of truth.

AISIS routing can incorporate recent latency, quota/rate-limit state, cost policy, and capability availability, but must not create an independent hidden retry tree that disagrees with OpenClaw.

The chosen route and any fallback are observable in traces without exposing credentials.

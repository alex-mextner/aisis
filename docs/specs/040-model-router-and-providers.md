# Model Router and Providers

## Provider neutrality

AISIS never makes a product feature depend on one vendor model identifier.

Users connect providers through BYOK credentials and map models into semantic aliases: `fast`, `balanced`, `deep`, and `background`.

## Initial provider adapters

The first provider set is Hugging Face, OpenRouter, OpenAI, and DeepSeek direct.

Additional OpenAI-compatible providers can reuse a transport adapter when their semantics match; vendor-specific features remain explicit extensions.

Local executors Codex, Claude Code, and OMP are reached through the Local Edge Agent.

## Route decision

A `RouteDecision` includes execution kind, model alias, reasoning effort, allowed tool groups, context budget, time budget, and fallback chain.

The routing chain is:
1. deterministic intent/tool rules;
2. optional local Laya decision model;
3. optional Jev decision provider;
4. conservative static fallback.

Laya and Jev implement the same `RouteDecisionProvider` protocol and can be A/B tested.

For Russian routing, the Laya multilingual checkpoint is the default Laya candidate; the English-only checkpoint must not be selected merely because it is the package root. Laya is intended to be preloaded to avoid cold-start latency.

Jev is optional and may be remote; loss of Jev never blocks deterministic routing or the configured static fallback.

## Effort

Reasoning effort is independent from model identity. Canonical effort levels are `none | low | medium | high | xhigh | max`; adapters map unsupported values to the nearest safe supported value.

The router should choose the minimum tier/effort that satisfies the task and deadline, then escalate on explicit uncertainty/failure signals.

## Fast and deep examples

A configured DeepSeek V4.1 Flash can serve the `fast` alias.

A configured GPT-6 Astra can serve `deep` for difficult work; another model may replace it without changing callers.

## User controls

Web/chat settings let the user add keys, test them, choose default aliases, cap spend, set fallback order, and disable providers.

Provider health, rate limits, latency, and cost are observed and fed into routing without exposing secrets.

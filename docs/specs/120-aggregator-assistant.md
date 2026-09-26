# Universal Aggregator Assistant

## Role

The aggregator is the user-facing assistant that composes calendar, finance, Home Assistant, personal connectors, public-information tools, and general models.

It does not reimplement domain logic. It selects and coordinates the appropriate domain capabilities.

## Example routing

“Что у меня сегодня?” → calendar deterministic/tool path.

“Сколько будет сто евро плюс 15 процентов?” → calculator path, no LLM required after parsing.

“Почему ночью было жарко и сделай сегодня комфортнее” → HA history/context + model plan + bounded HA actions.

“Что я пропустил в Слаке и почте?” → Slack/email read tools + summarization.

“Напиши Лене, что опоздаю” → Telegram recipient resolution + send policy.

“Разберись в этом проекте и вернись с выводами” → OpenClaw background task, potentially on a local Codex/Claude Code/OMP harness.

## Tool-group loading

The router first chooses coarse capability groups using deterministic signals plus Laya/Jev when useful.

Only tool names and concise titles are initially exposed. Detailed schemas/descriptions can be loaded on demand for selected groups.

This keeps prompts small and avoids presenting unrelated private capabilities to every model call.

## Multi-domain tasks

A single turn may span multiple domains. The planner may request several tool groups when the task genuinely needs them.

Cross-domain writes are ordered and individually audited. A failure in one domain does not erase successful side effects in another; the answer reports partial completion explicitly.

## Personality and continuity

Surface adapters should not invent separate personalities for each bot.

Domain bots may keep specialized command UX, but when routed through AISIS the assistant retains one conversation identity and can refer to prior turns and background tasks with the correct scope.

## General questions

Questions that do not need personal data stay on the general path.

The assistant should not load calendar, finance, Telegram, email, Notion, Slack, or HA context merely because those connectors exist.

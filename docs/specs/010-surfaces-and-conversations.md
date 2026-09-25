# Surfaces and Conversations

## Goal

Make Alice, Telegram, web, local edge clients, and the local voice speaker interchangeable presentations of one assistant rather than separate bots with duplicated logic.

## Normalized turn

Channel plugins turn every inbound request into an OpenClaw message. Where a domain service needs cross-surface semantics, AISIS represents it as a `ProductTurn` (`docs/architecture/contracts.md`) containing principal, conversation, surface context (including `deadline_at`), text, locale, and trace metadata. Capabilities come from the principal's resource grants, not from the turn.

Surface adapters may enrich a turn with transport facts, but domain tools cannot depend on transport SDK objects.

## Alice adapter

The Alice adapter parses Yandex Dialogs request JSON and produces a turn with a hard response deadline.

It maps `ProductAnswer.display_text` to Alice `response.text` and `ProductAnswer.speech_text` to `response.tts`.

If a tool/model cannot safely finish inside the remaining budget, the adapter returns a short acknowledgement tied to an OpenClaw background task, which AISIS links to the principal with a `RuntimeTaskBinding` (spec 050).

Buttons/deep links are generated from structured answer actions, including account-link and settings URLs.

## Alice activation

AISIS uses the maximum useful set of allowed activation-name variants for each published skill, but does not pretend a third-party skill can globally intercept arbitrary first-party phrases.

The universal assistant is the main conversational entry point. Calendar, calculator, and Home Assistant may also have focused skill entry points where that improves discoverability.

Inside an active skill, natural aliases such as “посчитай”, “подели”, “что у меня завтра”, and “что дома” are handled as intents without artificial phrases like “активируй навык калькулятор”.

## Telegram adapter

The Telegram adapter supports text, voice transcripts, rich messages, callbacks, status commands, and background-delivery receipts.

The aggregator bot is a new surface. Existing Calendar/Finance bots may continue to offer their specialized Telegram UX while sharing domain contracts.

Rich final output has a concise visible summary plus expandable details. The logical answer is stored independently from Telegram markup.

## Local speaker adapter

The `openclaw-speaker` channel plugin (spec 140) receives transcripts from a paired home speaker and speaks `ProductAnswer.speech_text`, or `ProductAnswer.display_text` rendered for speech.

Speaker turns have no platform deadline, so `deadline_at` may be empty; long work is acknowledged aloud and continues as an OpenClaw background task delivered per spec 050.

## Cross-surface conversations

A `Conversation` belongs to a principal, not to a chat platform. Each surface thread maps to a conversation scope.

Users may explicitly continue a conversation from another surface. The system can also associate a completed background task with the principal globally through its `RuntimeTaskBinding` so “что там с тем анализом?” works elsewhere.

Surface-private context such as a Telegram group must not silently leak into a private Alice response without an explicit context policy.

## Pending results on Alice

Because a standard Alice skill cannot initiate a dialog, completed results are placed in the user's pending-result inbox.

At the next suitable Alice turn, the assistant can say that a result is ready and ask whether to present it now.

Optional Station TTS delivery is a separate adapter. Its default is privacy-safe `announce_ready`; explicit per-Station `speak_full` opt-in permits the full result for tasks initiated on that Station.

## Deadlines

Each turn's surface context carries `deadline_at`; `None` means no deadline. The router receives the remaining budget and must not start a path whose p95 latency cannot fit.

The surface adapter reserves a rendering/serialization safety margin and can force conversion to a background task when budget becomes insufficient.

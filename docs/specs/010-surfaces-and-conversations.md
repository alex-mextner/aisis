# Surfaces and Conversations

## Goal

Make Alice, Telegram, web, and local edge clients interchangeable presentations of one assistant rather than separate bots with duplicated logic.

## Normalized turn

Every inbound request becomes a `Turn` containing principal, conversation, surface context, text/input payload, locale, deadline, capabilities, and trace metadata.

Surface adapters may enrich a turn with transport facts, but domain tools cannot depend on transport SDK objects.

## Alice adapter

The Alice adapter parses Yandex Dialogs request JSON and produces a turn with a hard response deadline.

It maps `Answer.display_text` to Alice `response.text` and `Answer.speech_text` to `response.tts`.

If a tool/model cannot safely finish inside the remaining budget, the adapter returns a short acknowledgement tied to a durable job.

Buttons/deep links are generated from structured answer actions, including account-link and settings URLs.

## Alice activation

AISIS uses the maximum useful set of allowed activation-name variants for each published skill, but does not pretend a third-party skill can globally intercept arbitrary first-party phrases.

The universal assistant is the main conversational entry point. Calendar, calculator, and Home Assistant may also have focused skill entry points where that improves discoverability.

Inside an active skill, natural aliases such as “посчитай”, “подели”, “что у меня завтра”, and “что дома” are handled as intents without artificial phrases like “активируй навык калькулятор”.

## Telegram adapter

The Telegram adapter supports text, voice transcripts, rich messages, callbacks, status commands, and background-delivery receipts.

The aggregator bot is a new surface. Existing Calendar/Finance bots may continue to offer their specialized Telegram UX while sharing domain contracts.

Rich final output has a concise visible summary plus expandable details. The logical answer is stored independently from Telegram markup.

## Cross-surface conversations

A `Conversation` belongs to a principal, not to a chat platform. Each surface thread maps to a conversation scope.

Users may explicitly continue a conversation from another surface. The system can also associate a completed job with the user globally so “что там с тем анализом?” works elsewhere.

Surface-private context such as a Telegram group must not silently leak into a private Alice response without an explicit context policy.

## Pending results on Alice

Because a standard Alice skill cannot initiate a dialog, completed results are placed in the user's pending-result inbox.

At the next suitable Alice turn, the assistant can say that a result is ready and ask whether to present it now.

Optional Station TTS delivery is a separate adapter. Its default is privacy-safe `announce_ready`; explicit per-Station `speak_full` opt-in permits the full result for tasks initiated on that Station.

## Deadlines

Each turn carries `deadline_at` or no deadline. The router receives the remaining budget and must not start a path whose p95 latency cannot fit.

The surface adapter reserves a rendering/serialization safety margin and can force conversion to a job when budget becomes insufficient.

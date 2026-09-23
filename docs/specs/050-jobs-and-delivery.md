# Durable Jobs and Delivery

## Purpose

Long-running work must not be represented as an HTTP request waiting forever.

A `Job` is a durable execution record that can survive process restart, ask the user questions, emit progress, and deliver final output later.

## Lifecycle

Canonical states are `queued`, `running`, `waiting_user`, `succeeded`, `failed`, and `cancelled`.

Jobs store the original request, principal, conversation reference, route/executor, tool grants, checkpoints, progress events, result, artifacts, and delivery receipts.

## Alice behavior

If a request will not fit within Alice's response budget, AISIS acknowledges immediately: work has started and can be queried by status.

The standard Alice skill does not rely on streaming a later continuation into the same response.

When the result is ready, the next Alice interaction may restore context and ask: “Ты спрашивал … Ответ готов. Рассказать сейчас или позже?”

## Telegram behavior

Telegram may receive progress updates and the final answer proactively.

For supported clients the preferred final form is one Rich Message with a visible summary and expandable details. Draft streaming can be used while an answer is actively produced.

## Optional Station delivery

A Station delivery adapter may send a privacy-safe ready notification through a locally connected HA/YandexStation path.

It is deliberately separate from Yandex Dialogs and disabled unless the user enables it.

## Status queries

Natural language status resolution finds active/recent jobs by explicit ID, current conversation, recency, and semantic reference to the original request.

If several jobs plausibly match, the assistant asks a compact disambiguation question.

## Idempotency

External side effects carry idempotency keys where the downstream service supports them.

Retries must never silently duplicate messages, calendar events, payments, or home actions.

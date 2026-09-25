# Durable Jobs and Delivery

## Runtime

Use OpenClaw background tasks, Task Flow, Automations, and Lobster approval/resume primitives rather than introducing a parallel AISIS job engine unless a proven gap requires one.

AISIS adds product-level metadata that links runtime tasks to a principal, originating surface, resource grants, and delivery preferences.

The core record is a `RuntimeTaskBinding` (`docs/architecture/contracts.md`): principal, conversation, runtime and runtime task id, original request, and originating surface. Results are delivered to `DeliveryTarget`s, each naming the same principal.

## Lifecycle

The product-level state maps onto runtime state and exposes at least:
`queued | running | waiting_user | succeeded | failed | cancelled`.

Jobs/tasks retain original request, conversation/session reference, selected executor/model/harness, progress, pending question/approval, result/artifacts, and delivery receipts.

## Alice behavior

A normal Alice webhook must finish within its response deadline; AISIS does not stream the later result into that HTTP response.

For long work, Alice immediately acknowledges that the task is running and can answer natural status queries.

Each bound Station has an explicit completion policy:

- `pending_only`: never speak proactively;
- `announce_ready` (default): privacy-safe completion notice;
- `speak_full`: speak the full completed answer for tasks initiated through that bound Station.

`speak_full` is a deliberate user opt-in for a trusted room/Station. It is allowed exactly to support the same-surface behavior requested for Alice, but it is never inferred from ordinary account linking or voice recognition.

If no valid originating Station binding exists, the result remains pending and is offered on the next Alice turn and/or delivered to other enabled targets such as Telegram.

## Local speaker behavior

Each paired local speaker (spec 140) has the same completion policy as a Station, set per speaker binding: `pending_only`, `announce_ready` (default) or `speak_full`.

`announce_ready` says only that a result is ready and where to read it, never its content. `speak_full` is a per-device opt-in for a trusted room and covers only tasks initiated through that speaker; results of tasks started on other surfaces are at most announced. It is never inferred from voice identification, and quiet hours apply.

A `LocalSpeakerDelivery` names the principal and the speaker binding. If that binding is gone or belongs to another principal, nothing is spoken and the result goes to other enabled targets such as Telegram.

## Telegram behavior

Telegram may receive progress and final results proactively.

Prefer a single Rich Message with a concise visible summary and expandable details. Preserve one logical answer instead of arbitrary 4096-character message chunks.

## Status queries

Natural-language resolution finds active/recent tasks by explicit ID, current session, recency, and semantic relation to the original request.

Ambiguous references require a compact choice rather than guessing.

## Idempotency

Retries must never duplicate external messages, calendar events, financial mutations/payments, home actions, or calls. Side-effecting workflow steps use idempotency keys or durable execution receipts when available.

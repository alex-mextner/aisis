# HyperCalendarBot as Calendar Domain Service

## Decision

Do not build a second calendar engine in AISIS.

HyperCalendarBot remains the calendar product and gains a surface-neutral entry contract so Telegram and Alice can drive the same deterministic intent, agent, workflow, sharing, and Google Calendar code.

## Current architecture implication

HyperCalendarBot already has a layered pipeline with deterministic intent matching before the AI agent, plus a large capability-oriented `AgentContext`.

The refactor target is to separate transport facts from calendar interaction facts, not to rewrite the pipeline.

## Neutral calendar turn

Introduce a calendar-domain request model containing principal/user mapping, text, locale/timezone, conversation reference, input mode, and optional response capabilities.

Telegram's existing context becomes one adapter. AISIS/Alice becomes another.

Telegram-only callbacks, message IDs, keyboards, group topics, and reactions remain optional surface capabilities rather than calendar-core requirements.

## Calendar tools

AISIS should be able to invoke calendar operations such as agenda lookup, event search, create/edit/delete, free/busy, invitations, sharing, reminders, and Google connection status.

Where HyperCalendarBot already has a deterministic intent for a request, that path remains preferred over a fresh general LLM interpretation.

## Google account linking

The Alice flow opens AISIS account linking on the phone, links the AISIS principal, and then reuses HyperCalendarBot's Google Calendar connection capability.

A single linked calendar identity must be usable from both Telegram and Alice after explicit identity association.

## Cross-person calendar

“Что у Лены завтра?” and “когда мы оба свободны?” use HyperCalendarBot sharing/delegation rules.

The assistant never infers calendar permission from voice recognition or household membership.

## Migration safety

Existing Telegram behavior and deployment remain intact during adapter extraction.

The new adapter requires contract tests proving equivalent calendar outputs for representative Telegram and Alice turns.

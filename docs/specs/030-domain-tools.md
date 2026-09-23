# Domain Tools and Existing Bots

## Boundary rule

AISIS orchestrates domains; it does not become the database or business-logic owner for every domain.

Each domain exposes a typed capability interface and can remain independently deployable.

## Calendar

HyperCalendarBot is the calendar domain service. It owns calendar-specific intents, event logic, Google Calendar sync, sharing/invitations, and calendar persistence.

AISIS needs a surface-neutral adapter around its existing pipeline/tool layer rather than a second calendar implementation.

## Finance

ExpenseSyncBot is the finance domain service. It owns expenses, budgets, bank integrations, finance-specific currency behavior, and finance persistence.

AISIS can invoke finance tools through an adapter and reuse the generic calculator semantics/test corpus.

## Home Assistant

Home Assistant is a new AISIS domain adapter with both read and action tools.

It intentionally does not implement Yandex Smart Home device discovery/control because that path already exists via `dext0r/yandex_smart_home`.

## General questions

General Q&A uses the model gateway plus explicitly enabled public-information tools. It has no implicit permission to query personal resources.

The router includes only relevant tool groups for each turn to reduce latency, context size, and accidental actions.

## Tool contract

Every tool declares:
- stable name and version;
- JSON-schema-compatible input/output types;
- capability/resource requirements;
- risk class and idempotency;
- latency class;
- whether it can run synchronously on Alice;
- human-readable title/description loaded on demand.

Tool transport may be in-process, HTTP, or MCP, but transport is not part of the semantic contract.

## Failure semantics

Tools return typed success or typed failure with retryability and safe user-facing summary.

An LLM is never asked to infer whether a tool succeeded from free-form logs.

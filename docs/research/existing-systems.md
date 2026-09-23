# Existing Systems and Reuse Notes

## HyperCalendarBot

Repository: `alex-mextner/HyperCalendarBot`.

Observed architecture includes a layered Telegram pipeline: feedback routing, deterministic intent matching/workflow execution, then AI agent fallback.

The agent context already groups many capabilities but still carries Telegram-specific fields. AISIS should extract a surface-neutral request/response seam while preserving the existing calendar engine.

## ExpenseSyncBot

Repository: `alex-mextner/ExpenseSyncBot`.

The calculator uses Big.js, a safe parser without `eval`, operator precedence, percentage handling, currency conversion, and precision-oriented tests.

AISIS should reuse its semantics and regression cases. The new rendering/rationalization layer is additive and surface-neutral.

## dext0r/yandex_smart_home

This already solves Home Assistant → native Yandex Smart Home exposure for the user.

AISIS explicitly does not compete with it.

## AlexxIT/YandexDialogs

This is primarily an Alice skill webhook bridge into Home Assistant, not the native Smart Home integration.

Useful for prototyping/reference; not required when AISIS owns public ingress and reaches HA privately through edge.

## AlexxIT/YandexStation

This is relevant to the opposite direction: controlling/speaking through Yandex Stations from Home Assistant.

It can later back optional proactive Station notifications when a long AISIS job finishes.

## Local routing research

Laya is suitable as a local compact route-decision model with request-defined choices.

Jev is suitable as a decision model for structured choices such as model tier and reasoning effort.

Both must sit behind one replaceable `RouteDecisionProvider` interface.

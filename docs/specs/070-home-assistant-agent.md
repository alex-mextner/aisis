# Home Assistant Conversational Domain

## Non-goal

AISIS does not replace `dext0r/yandex_smart_home` and does not reimplement native Yandex Smart Home exposure.

Direct commands such as “Алиса, включи свет” should continue to use the user's existing integration.

## Goal

Provide an AI-capable HA domain that works from both the AISIS Telegram surface and Alice skill.

Examples include compound commands, questions requiring several entity states, history-based analysis, explanations, and plans involving multiple services.

## Connectivity

The known HA instance is reachable from the user's Tailnet at `http://home.tailbfe8ea.ts.net:8123`.

It is not assumed to be publicly reachable. The preferred path is AISIS cloud/core → outbound Local Edge Agent → private HA API.

A colocated deployment inside the Tailnet may use the same typed adapter directly.

## Tool groups

Read tools cover areas/entities, state, attributes, history/statistics, scripts/scenes metadata, and capability discovery.

Action tools cover service calls, scene/script execution, and compound plans.

The model receives a filtered home context relevant to the query rather than the entire HA state dump.

## Action safety

The adapter classifies actions by risk. Lighting/climate may be configured as low risk; locks, alarms, garage doors, and other sensitive actions default to confirmation.

Compound plans are validated before execution and return per-step results.

## Telegram and Alice

Both surfaces call the same HA domain protocol.

The HA-specific Telegram bot may expose specialized commands/debugging, while the universal aggregator can call HA as one tool group.

## YandexDialogs reference

AlexxIT/YandexDialogs is useful as reference code for Alice → HA: it turns Home Assistant into a Yandex Dialogs webhook and routes phrases into HA automations/intents.

AISIS does not depend on that topology because it would require making HA itself reachable as the Dialogs webhook; our public ingress plus private edge path keeps HA private.

# Home Assistant Conversational Domain

## Non-goal

AISIS does not replace `dext0r/yandex_smart_home` and does not reimplement native Yandex Smart Home exposure.

Direct commands such as “Алиса, включи свет” continue through the existing integration.

## Goal

Provide AI-capable Home Assistant tools for compound, contextual, analytical, historical, and multi-step requests from the central assistant.

## Connectivity

Known, verified endpoints:
- Tailnet: `http://home.tailbfe8ea.ts.net:8123`;
- public HTTPS proxy: `https://spry-gazelle-4693.dataplicity.io/` (HTTP 200 verified from the development machine on 2026-09-23).

The adapter supports both. Tailnet/local transport is preferred for private/high-trust access; HTTPS is useful for server deployments that cannot join the Tailnet.

Secrets are never stored in the public repository.

## Runtime reuse

Hermes's HA integration is a reference for entity/service tools and filtered WebSocket `state_changed` events.

OpenClaw remains the central runtime; AISIS can implement HA as an OpenClaw tool/plugin or connect an existing supported HA/MCP integration.

## Tool groups

Read tools cover areas/entities, states/attributes, history/statistics, scripts/scenes metadata, services, and capability discovery.

Action tools cover service calls and validated compound plans.

The model receives filtered relevant state rather than the entire home state.

## Safety

Actions are risk-classified. Locks, alarms, garage doors, and similarly sensitive operations default to confirmation.

The existing native Yandex Smart Home path remains the fastest route for ordinary direct commands.

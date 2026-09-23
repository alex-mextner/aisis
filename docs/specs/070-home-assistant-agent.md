# Home Assistant Conversational Domain

## Non-goal

AISIS does not replace `dext0r/yandex_smart_home` and does not reimplement native Yandex Smart Home exposure.

Direct commands such as “Алиса, включи свет” continue through the existing integration.

## Goal

Provide AI-capable Home Assistant tools for compound, contextual, analytical, historical, and multi-step requests from the central assistant.

## Connectivity

Home Assistant endpoints are deployment configuration, never repository constants.

Supported transport profiles are:

- `tailnet`: a private HTTP/HTTPS endpoint reachable from the paired ORC device or trusted server;
- `https`: a public HTTPS reverse proxy protected by Home Assistant bearer authentication.

Configuration uses SecretRefs/environment settings such as `HA_BASE_URL` and `HA_TOKEN`. Public documentation uses non-live examples only.

The same typed HA adapter must work over both profiles. Tailnet/local access is preferred for private/high-trust operations when available.

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

# Alice Platform Findings

## Response timing

Yandex Dialogs requires the webhook to produce a complete response within the platform deadline. A normal skill cannot send “секундочку” and then stream a later continuation into the same webhook response.

The design consequence is durable jobs plus status/result recall.

## Separate display and speech

Alice responses expose separate `text` and `tts` fields.

This directly supports AISIS's universal `display_text` / `speech_text` contract and makes human-friendly calculator speech possible without sacrificing exact screen representation.

## Conversation initiation

A standard skill conversation is user-initiated. The skill responds to requests; it does not independently wake and start a normal Dialogs conversation.

Proactive result notification on a Station therefore requires a separate, explicitly configured delivery path.

## Speaker recognition

Alice devices may recognize household voices for first-party UX, but that mechanism is not a reliable third-party authorization boundary.

The public skill protocol should be designed around linked account identity and explicit resource ACLs.

## YandexDialogs by AlexxIT

`AlexxIT/YandexDialogs` is a Home Assistant custom component that makes HA itself handle a Yandex Dialogs skill webhook and routes phrases to HA automations/intents/scripts.

It is useful reference code for the Alice → HA direction.

For AISIS it is not the preferred production topology: the user's HA is private on Tailscale, while AISIS already needs a public multi-domain ingress. The ORC edge/desktop transport (spec 100) can keep HA private and still expose typed HA tools.

## Sources

- Yandex Dialogs developer documentation: response format, waiting/slow-response guidance, activation behavior.
- https://github.com/AlexxIT/YandexDialogs
- https://github.com/AlexxIT/YandexStation

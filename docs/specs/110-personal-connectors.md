# Personal Resource Connectors

## Common contract

Telegram, email, Notion, and Slack implement a common connector shape for authorization, capability discovery, search/read, optional write, health, and revocation.

Each connector publishes exact scopes and surfaces them to the user before authorization.

## Email

V1 targets OAuth-capable mail first, with Gmail as the primary implementation.

Capabilities include thread/message search and read plus optional draft/send. Send access is separate from read access.

## Notion

Capabilities include search/fetch of accessible pages and optional create/update in explicitly granted locations.

The assistant should preserve source links in answers and avoid indexing an entire workspace unless the user asks for that behavior.

## Slack

Capabilities include workspace/channel/thread search and read plus optional posting.

Private channels remain subject to provider permissions; the assistant never treats workspace membership as access to all channels.

## Telegram

Telegram personal access is specified separately because MTProto session security and recipient resolution are materially different from typical OAuth APIs.

## Tool exposure

Connectors expose narrow semantic tools rather than one generic “call arbitrary provider API” tool.

The router only loads connectors/tool groups relevant to the current request.

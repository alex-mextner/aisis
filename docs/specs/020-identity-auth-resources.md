# Identity, Authorization, and Personal Resources

## Principal model

A `Principal` is the assistant's stable person identity. External accounts are linked as `ExternalIdentity` records.

Examples include Alice account subject, Telegram user ID, Telegram MTProto account, Google account, Slack member, Notion user, email identity, edge-device identity, and paired local speaker devices.

No provider-specific ID is used as the global primary key.

## Alice and speaker identity

Alice `user_id` can link an account but must not be treated as proof of a physical speaker.

Speaker hints, such as the local speaker's voice identification (spec 140), are advisory only: they may select preferences (music account, persona), never the principal, grants or confirmation requirements. A local speaker turn's principal comes from device pairing. Sensitive calendar, message, finance, or home actions authorize against the linked principal and ACL.

## Account linking

Alice can initiate a secure account-link flow that opens on the user's phone. The web flow then links Google or other providers with normal OAuth.

The desired UX is one tap to continue with the external provider; no copy/paste tokens or codes when the provider flow can avoid them.

Telegram can issue the same short-lived setup link.

## Resource graph

Resources are first-class objects: calendars, Telegram accounts/chats, mailboxes, Slack workspaces/channels, Notion workspaces/pages, HA instances, edge devices.

A grant links a principal to a resource with explicit capabilities such as `read`, `search`, `create`, `update`, `delete`, `send`, or `admin`.

Cross-person calendar access uses explicit grants and delegation.

## Secrets

Secret material is stored through a secret-store interface, encrypted at rest, and addressed by opaque reference.

Raw tokens are never serialized into conversation history, job prompts, model context, logs, or telemetry.

For edge-owned resources, the cloud stores only a device-scoped capability reference; the secret remains local.

## Confirmation policy

Actions have a risk class. Read-only operations usually run immediately.

External communication, destructive changes, finance-related mutations, and ambiguous-recipient writes can require confirmation.

Voice-only turns (Alice, Stations, the local speaker, calls) never authorize tools with risk `sensitive`, `destructive` or `external_write`, finance mutations or payments, lock/alarm/garage/door actions, or messages sent as the user. A spoken confirmation such as «да» does not count; these actions require approval on an authenticated surface such as Telegram or web.

A user may configure trusted low-risk automations, but every bypass is scoped and auditable.

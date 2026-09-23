# Alice Publication and Testing Notes

## Private-first workflow

New skills should begin as private skills so activation, account linking, error paths, screen/no-screen responses, and real Station behavior can be tested before catalog publication.

The test matrix must include at least a Station, Alice mobile surface, authorized and unauthorized users, account-linking expiry, and the 4.5-second deadline path.

## Publication

A catalog skill is switched to public access only after the private test suite and moderation-oriented UX checks pass.

Publication metadata is Russian-first: name, activation names, description, examples, privacy links, and support contact.

The public skill must implement clear “Помощь” / capability-discovery behavior and graceful errors without exposing internal model/provider terminology.

## Authentication

OAuth/account linking is part of the product flow rather than an installation instruction.

A Station-originated request should be able to hand the user to a phone/browser page, complete provider OAuth, and return to a linked AISIS principal without copy/paste secrets.

## Moderation readiness

The release checklist treats Yandex moderation as an external compatibility gate, not as the primary correctness test.

Every change to activation, authentication, requested permissions, response format, or public metadata is regression-tested privately before resubmission.

## Sources

- https://yandex.ru/dev/dialogs/alice/doc/ru/activation
- https://yandex.ru/dev/dialogs/alice/doc/ru/publish-settings
- https://yandex.ru/dev/dialogs/alice/doc/ru/response

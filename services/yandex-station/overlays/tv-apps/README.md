# TV app commands for AISIS

This overlay adds explicit Netflix and YouTube commands to the existing fast intent layer. The target is always the living-room TV/Chromecast pair, including requests spoken to the Studio AISIS speaker.

Accepted commands use «открой», «включи» or «запусти» with Netflix/Нетфликс or YouTube/Ютуб/Ютюб/Ютьюб, optionally «на телевизоре»/«на ТВ»/«на телеке»/«на телике» and «пожалуйста». Complete-phrase matching rejects questions, negations, music requests, off/pause/volume commands and compound requests.

The layer invokes exactly one `script.turn_on` call for `script.living_room_netflix` or `script.living_room_youtube`. Those HA scripts own power synchronization and launch. AISIS observes `media_player.televizor` afterward. An active state plus the expected `app_id` confirms success; old activity retained while off, stale HA cache, unavailable data or another app do not. Observation is bounded; an accepted launch that is still pending gets an explicit unconfirmed reply. An uncertain request is never retried.

## Source provenance

The baseline was read from the actual active `aisis-quasar-prod-blue` image, not the separately modified source cache.

- Image: `sha256:b05533500fe7e99c90cb47137ac3a5af99aaa71034fc3ccb01dbaa69e72661f9`
- `/src/backend/ha-intents.ts`: `d27745ec7670384fe960b409f3ccdfed4f1043773a31a8c9b38671041c85cebc`
- Reviewed patched matcher: `01585aa3b35ffa86bb3f659cfc4d42686d3844c3e1c6b2ddda8f73319b358f07`

`prepare_overlay.py --baseline FILE --output-dir NEW_DIRECTORY` checks that baseline, applies the exact small patch with no fuzz, checks the result and copies the new helper. It emits a source manifest and private automatic execution log. It does not deploy or launch devices.

The existing blue/green stage, corpus, dialog, idle-color readiness and switch checks remain required. Full corpus and adversarial execution belongs on the managed RunPod runner; the image and all executed source/dependency bytes must match the stage/idle candidate before switching.

## Targeted tests

On the managed RunPod runner, use Node26 and TypeScript5.9.3 for the actual-matcher contract harness:

`AISIS_ACTIVE_MATCHER_SOURCE=/path/to/captured-or-patched-ha-intents.ts node --experimental-vm-modules --test tests/*.mjs`

The actual-matcher tests evaluate the captured production matcher with external service modules isolated; they prove the new dispatch location, exact nonblocking service payload, and error/cache boundaries. Pure tests exercise the complete phrase grammar and bounded one-shot action. They complement the original corpus/dialog gates and do not prove physical app launch.

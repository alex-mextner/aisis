# Repository Layout

AISIS is a public integration/distribution monorepo around OpenClaw plus independently versioned product subrepositories.

```text
aisis/
  apps/
    web/                         # setup, identities, providers, jobs, workflows
    speaker/                     # local voice speaker client (spec 140): vad-ctl events + in-process stt-lib (Python), talks to openclaw-speaker
  packages/
    openclaw-alice/              # Yandex Alice channel plugin
    openclaw-speaker/            # local voice speaker channel plugin (spec 140)
    openclaw-aisis-tools/        # aggregator/domain connector tools
    openclaw-routing/            # Laya/Jev routing extensions
    calculator/                  # reusable @aisis/calculator
    rendering/                   # display/speech formatting
    identity/                    # cross-service principal/resource mappings
    workflow-ir/                 # typed graph/workflow contracts + compilers
    call-client/                 # central call API client/contracts
    telegram-personal/           # MTProto connector / resolver
  services/
    calls/                       # Telegram P2P call service during migration
  subrepos/
    calendar/                    # git submodule: HyperCalendarBot
    finance/                     # git submodule: ExpenseSyncBot
    vibeflow/                    # git submodule: VibeFlow
    desktop/                     # git submodule: Open Remote Commander (alex-mextner/open-remote-commander)
  docs/
    specs/
    architecture/
    research/
    superpowers/plans/
```

## Upstream OpenClaw

OpenClaw is consumed as a released dependency/plugin host. It is not vendored as a git submodule by default.

If a required change cannot be expressed through the public Plugin SDK, maintain the smallest possible patch/fork and attempt to upstream it.

## Language/tooling

OpenClaw/AISIS plugins, existing domain bots, VibeFlow, and the shared calculator use TypeScript.

Use the package manager/runtime required by each upstream project; do not rewrite mature TypeScript services merely to make the repository single-language.

Python remains acceptable for the existing Telegram-call bridge and specialized ML/media components, including the local speaker client `apps/speaker`, which embeds `stt-lib`.

The desktop transport is already Go in Open Remote Commander (ORC, `alex-mextner/open-remote-commander`); remaining work is product integration, harness APIs, signed installers, and auto-update.

## Subrepo rules

Submodules point at reviewed commits.

Changes to HyperCalendarBot, ExpenseSyncBot, or VibeFlow are developed in their own worktrees/branches/PRs first; the AISIS submodule pointer updates only after those commits are reviewable.

Each subrepo must remain buildable/deployable on its own.

## Deployment

OpenClaw Gateway is the central runtime.

HyperCalendarBot and ExpenseSyncBot expose authenticated service APIs and can retain independent workers/databases.

VibeFlow can run independently or be embedded behind AISIS web/navigation.

Telegram P2P calls may run as a separate service while their dependencies remain Python/native-heavy.

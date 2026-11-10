# yandex-station — live Yandex Station adapter

The rooted Yandex Station Mini in the living room is **one adapter** of the AISIS
voice surface (more station adapters may follow): it donates microphone, speaker,
compute, LED and hardware controls to AISIS. Everything the speaker hears or says
flows through the stack in this directory.

This directory holds **code only** — Python/shell sources, blue/green deploy
tooling, the compose file and the systemd units. Runtime state (venvs, sqlite
databases, models, corpora, prompts, voice archive, blue/green state) is **NOT in
the repo**; it lives under `~/.local/share/aisis/` (plus `~/.config/aisis/`,
`~/.cache/aisis/`, `~/.local/state/aisis/`).

## Dependency chain

```text
Yandex Station Mini (rooted, LAN 192.168.0.56)
  └─ quasar.yandex.net/.ru + scbh → tls-voice (443, aisis-yandex network compose)
       └─ switch-prod nginx :31115/31116/31114 (project aisis-voice, bluegreen/)
            └─ active quasar color: aisis-quasar-prod-blue :31215/31216/31214
               or            aisis-quasar-prod-green :31315/31316/31314
                 ├─ postgres aisis-yandex-postgres-1 (127.0.0.1:31119)
                 ├─ adapter aisis-station-adapter :31117  (ChatGPT + Bracho TTS + processor)
                 │    ├─ pm aisis-station-pm :31118        (speech prompts via ChatGPT browser CDP)
                 │    ├─ embed aisis-station-embed          (e5-small ONNX memory sidecar)
                 │    └─ ha-cli :8795                      (Home Assistant proxy w/ state cache)
                 └─ stt aisis-station-stt :31121            (streaming gateway → Wyoming whisper
                                                            tcp://100.75.131.84:10300, CUDA)
support services: aisis-station-kit :18080 (eQuasar sync server, ~/lite-kit),
  aisis-station-lan-proxy (192.168.0.56:31116 → 127.0.0.1:31116), aisis-clipfeed
  (Telegram clip feed + ASR truth), aisis-dashboard :8781, aisis-failover (watchdog)
```

## Ports

| Port  | Service | Unit / container |
|-------|---------|------------------|
| 31117 | adapter (processor + TTS/v1) | aisis-station-adapter.service |
| 31118 | pm (ChatGPT browser CDP) | aisis-station-pm.service |
| 31121 | streaming STT gateway | aisis-station-stt.service |
| 8795  | Home Assistant CLI proxy | aisis-ha-cli.service |
| 8781  | dashboard | aisis-dashboard.service |
| 31119 | postgres (loopback) | aisis-yandex-postgres-1 |
| 31115/31116/31114 | quasar switch (uniproxy/API/f-state) | aisis-switch-prod |
| 31215…/31315… | quasar blue/green prod colors | aisis-quasar-prod-{blue,green} |
| 31415… | stage switch + colors | aisis-switch-stage, aisis-quasar-stage-* |
| 18080 | station kit sync server | aisis-station-kit.service |
| 31116 (LAN) | quasar LAN proxy | aisis-station-lan-proxy.service |
| 10300 | Wyoming faster-whisper (tailnet) | wyoming-whisper.service |

## Ops runbook

- **Deploy units**: `scripts/install-systemd-units.sh` (repo root) copies every
  unit and drop-in of `systemd/` into `~/.config/systemd/user/`, reloads systemd,
  enables each unit with an `[Install]` section and starts the ones not running;
  it never restarts a running unit (it names the ones to restart). `--check` exits 3
  on drift, `--plan` only prints it. A fresh machine gets it from the `aisis`
  module of the `.files` profile (clones this repository, runs the installer).
- **State lives in**: `~/.local/share/aisis/` (venvs `venv/`, `embed-venv/`,
  `dsp-bench-venv/`, `bluegreen-state/`, `corpus/`, `models/`, `prompts/`,
  `releases/`, `voice-archive/`, `dashboard-ui/`, `pm-deps/`, `dialogs.sqlite3`,
  `ha-state-cache.json`, …), `~/.config/aisis/` (runtime.env, whisper-prompt.env,
  fish.key, per-feature json), `~/.cache/aisis/` (quasar-server source, fillers),
  `~/.local/state/aisis/` (adapter/pm sqlite, quasar-prod container state).
  Secrets (fish.key, runtime.env, *.env with tokens, sqlite) are never committed.
- **Blue/green deploys**: `bluegreen/deploy.sh deploy --overlay SRC_DIR …`;
  the deploy never touches the active color until stage + idle-color checks pass.
  `deploy.sh rollback` flips back. `bgctl.py status` shows the current colors.
- **Failover**: `aisis-failover.service` runs `bluegreen/failover.py --env prod`,
  probing adapter/stt/pm/ha-cli/kit/lan-proxy/embed units and the postgres,
  tls-voice, dns-relay containers. A `maintenance` file in
  `~/.local/share/aisis/bluegreen-state/` suspends it during deploys.
- **Whisper prompt**: `aisis-whisper-prompt.timer` regenerates the initial
  prompt from HA entities/artists every 6 h (code/refresh-whisper-prompt.sh).
- Containers keep their historical names (`aisis-yandex-postgres-1`,
  `aisis-yandex-network-{tls-voice,dns-relay}-1`, compose project `aisis-yandex`
  for postgres) — do not recreate them just to rename.

### Install snippet

```bash
~/xp/aisis/scripts/install-systemd-units.sh            # install + enable + start what is not running
~/xp/aisis/scripts/install-systemd-units.sh --check    # drift report, exit 3 on drift

# one-time rename of the pre-consolidation units (only on a machine that still has the old names):
#   aisis-yandex-adapter→aisis-station-adapter, -stt→aisis-station-stt,
#   -pm→aisis-station-pm, -embed→aisis-station-embed,
#   aisis-quasar-lan-proxy→aisis-station-lan-proxy, aisis-kit→aisis-station-kit
for old in aisis-yandex-adapter aisis-yandex-stt aisis-yandex-pm aisis-yandex-embed aisis-quasar-lan-proxy aisis-kit; do
  systemctl --user disable --now "$old.service"
done
```

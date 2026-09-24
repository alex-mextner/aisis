# Local Voice Speaker Surface

## Goal

Provide a self-hosted smart speaker as an AISIS surface: an alternative to Alice for the home, with our own wake word, voice activity detection and speech recognition running on the home server, and the same identity, conversations, tools and jobs as every other surface.

The speaker is presentation and transport only (spec 010). Wake word, VAD, STT and audio playback stay on the home box; the core receives a `Turn` with text and returns an `Answer` whose `speech_text` is spoken.

## Non-goals

The speaker does not replace Yandex Station or `dext0r/yandex_smart_home`; both keep working in parallel.

Speaker voice is never an authorization boundary (specs 000, 020). Speaker verification is used only to reject false wakes, never to grant access.

The speaker does not run an LLM locally by default; answers come from the AISIS core (or the central backend the core is configured with).

## Hardware baseline

The first deployment is the `home` Linux box (Ubuntu 26.04, i7-11800H, 14 GB RAM, RTX 3060 Laptop 6 GB) with a ReSpeaker USB Mic Array v2.0 (XMOS XVF-3000, 6-channel firmware: ch0 processed with AEC/beamforming/NS/AGC, ch1-4 raw mics, ch5 playback reference, DOA and VAD readable over USB tuning).

The GPU is shared with other services; the speaker stack must fit in about 3 GB of VRAM and degrade to CPU models when the GPU is busy.

## Components and repositories

| Component | Repository | Role |
|---|---|---|
| `vad` / `vad-ctl` | `ultrabricks/vad-cli` (Forgejo) | Resident audio daemon: mic capture, front-end, DOA, VAD, wake word stage 1 and stage 2, event socket, Wyoming wake/VAD endpoints, wake-word training and `wakebench` |
| `stt-lib` / `stt` | `ultrabricks/stt-cli` (Forgejo) | Speech recognition core as a library plus the CLI; streaming API for utterances; Linux/CUDA engines; self-install of engines and models |
| `apps/speaker` | this repository | The surface: subscribes to `vad-ctl` events, sends the utterance to `stt-lib`, builds a `Turn`, speaks the `Answer`, handles barge-in, follow-ups and media ducking |

The mic capture and wake word stay in one resident process so the wake decision and the command audio share one ring buffer and one clock.

## Audio pipeline

1. Capture 6-channel 16 kHz from the ReSpeaker; ch0 is the primary stream, ch1-4 feed DOA/energy, ch5 is the reference for what the array itself played.
2. Front-end: frozen XVF tuning; reference-based echo cancellation for media played by this host (Kodi, Music Assistant) through PipeWire `module-echo-cancel`; media ducking on wake.
3. VAD (streaming, CPU): Silero VAD v6 as the default gate; FireRedVAD streaming is evaluated as a replacement for its lower false-alarm rate.
4. Wake stage 1 (CPU): a small detector tuned for recall, trained on this home's audio.
5. Wake stage 2 (about 300 ms, all must pass):
   - ASR confirmation of the 2-3 s wake buffer with a phonetic fuzzy match of the phrase;
   - speaker verification against enrolled household voices (ONNX embeddings, CPU);
   - direction check: reject wakes whose median DOA falls inside the configured TV sector;
   - stricter thresholds while media is playing (state from Home Assistant / the local player).
6. Utterance capture until VAD end-of-speech (hangover about 700 ms, cap 15 s), then final STT.

Training data must match inference input: every model in the chain is trained and evaluated on the same processed stream it sees in production, including the AGC/noise-suppression stage.

## Wake phrase

The phrase is a configuration value, not code. Requirements: 3-4 syllables, at least 7 phonemes, stressed clear vowel, at least one sound that is rare in Russian speech (дж, ж, ф, ц), not a common word, name or brand heard on TV.

Candidates are screened before training by searching transcripts of the household noise recordings and public Russian speech corpora for phonetic near-matches.

The first model (`ey_milosh`) failed exactly on these criteria: two syllables, reduced unstressed vowel, a prefix shared with common words (мило, милый, миллион).

## Acceptance metrics

Every model or pipeline change is accepted only through benchmarks; numbers are recorded in the repository that owns the component.

Wake word:
- false accepts of the full two-stage chain at most 0.05 per hour on at least 100 hours of household ch0 audio recorded with no wake phrase spoken, evaluated on both the raw and the AGC-processed stream;
- stage 1 alone at most 2 false accepts per hour on the same audio;
- false-reject rate at most 5% at that operating point on at least 300 real utterances from at least 3 household speakers at 1-4 m, with and without TV;
- the benchmark must pass its own sanity gates: no silent audio, non-constant model output, and a replay test that reproduces logged production detections at their exact times.

VAD: frame-level false-alarm rate and F1 on a labeled household set (speech, TV speech, music, silence), plus FLEURS-VAD and ESC-50 as public references.

STT: WER on at least 300 household commands captured from ch0 (TV off and on) and on Golos farfield/crowd; end-of-speech to final text p50 at most 300 ms and p95 at most 700 ms on the home box.

End to end: wake-to-first-audio latency and task success on a scripted command set.

## Model choices (September 2026 survey)

- Wake stage 1: LiveKit wakeword and openWakeWord are compared on `wakebench`; microWakeWord is a fallback.
- STT: GigaAM-v3 (MIT) is the primary Russian engine; Whisper large-v3-turbo stays as a second opinion and for long-form; Vosk small-streaming on CPU gives partials.
- Speaker verification: WeSpeaker ResNet34 or CAM++ via ONNX.
- A rented GPU is used only for training runs; always-on inference stays on the home box.

## Integration with Home Assistant

The existing Wyoming satellite path keeps working during migration: `vad-ctl` exposes a Wyoming wake endpoint so HA Assist can use the new detector before `apps/speaker` is ready. `wyoming-satellite` is archived upstream (January 2026); the long-term HA path is `linux-voice-assistant` or `apps/speaker` itself.

## Deployment

The speaker runs as a colocated trusted worker inside the Tailnet (specs 070, 100): outbound connections only, secrets and raw audio stay on the device. Raw audio is kept only in explicit, time-boxed collection sessions and in bounded debug buffers with retention limits.

## Rollout

1. Honest benchmark and hard-negative mining on real household audio (`wakebench`).
2. New phrase: synthetic plus real positives, stage 1 model, stage 2 verifier; ship behind the Wyoming endpoint.
3. `stt-lib` on Linux/CUDA with GigaAM-v3; streaming utterance API.
4. `apps/speaker` end to end with TTS, ducking and follow-ups.
5. Front-end improvements: media reference AEC, TV sector rejection, speaker enrollment UI.

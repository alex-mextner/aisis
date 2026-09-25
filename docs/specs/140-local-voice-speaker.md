# Local Voice Speaker Surface

## Goal

Provide a self-hosted smart speaker as an AISIS surface: an alternative to Alice for the home, with our own wake word, voice activity detection and speech recognition running on the home server, and the same identity, conversations, tools and jobs as every other surface.

The speaker is presentation and transport only (spec 010). Wake word, VAD, STT and audio playback stay on the home server; the transcript enters the OpenClaw Gateway as a channel message through the `openclaw-speaker` channel plugin, and the reply's `speech_text` (or `display_text` rendered for speech per spec 090) is spoken.

## Non-goals

The speaker does not replace Yandex Station or `dext0r/yandex_smart_home`; both keep working in parallel.

The speaker does not run an LLM locally by default; answers come from the OpenClaw Gateway / main agent (specs 000 §2, 035), routed per spec 040.

## Hardware baseline

The reference deployment is a home Linux server whose 6 GB laptop-class NVIDIA GPU is shared with other services, plus a ReSpeaker USB Mic Array v2.0 (XMOS XVF-3000, 6-channel firmware: ch0 processed on-chip with AEC/beamforming/NS/AGC, ch1-4 raw mics, ch5 playback reference, DOA and VAD readable over USB tuning).

The speaker stack must fit in about 3 GB of VRAM and degrade to CPU models when the GPU is busy.

## Components and repositories

| Component | Repository | Role |
|---|---|---|
| `vad` / `vad-ctl` | `vad-cli` (separate repository, Python) | Resident audio daemon and the only owner of the mic capture: front-end, DOA, VAD, wake word stage 1 and stage 2, local event socket, Wyoming satellite endpoint, wake-word training and `wakebench` |
| `stt-lib` / `stt` | `stt-cli` (separate repository, Python) | Speech recognition core as a library plus the CLI; streaming API for utterances; Linux/CUDA engines; self-install of engines and models from pinned URLs with SHA-256 hashes |
| `apps/speaker` | this repository (Python) | Local client: subscribes to `vad-ctl` events, receives the utterance audio over the same socket, runs `stt-lib` in-process, sends the transcript to `openclaw-speaker`, plays TTS, handles barge-in, follow-ups and media ducking |
| `packages/openclaw-speaker` | this repository (TypeScript) | OpenClaw channel plugin: device pairing and principal binding, channel messages into the Gateway, display/speech separation, voice-authority policy, proactive delivery (`LocalSpeakerDelivery`) |

The mic capture and wake word stay in one resident process so the wake decision and the command audio share one ring buffer and one clock. `apps/speaker` is Python because it embeds the ML/media library `stt-lib` (repo-layout: Python is acceptable for specialized ML/media components).

## Gateway path

`apps/speaker` talks only to `openclaw-speaker`, a channel plugin loaded by the OpenClaw Gateway (specs 000 §6, 035). The plugin endpoint binds to loopback, or to a Tailnet-ACL'd address when the speaker runs on another machine, and authenticates `apps/speaker` with the device key issued at pairing.

The plugin turns each utterance into an OpenClaw channel message from the paired device, so session continuity, memory, tool policy, approvals and delivery stay with OpenClaw (contracts: "Surface-neutral product turn"). Like the Alice plugin (spec 000 §10), it keeps display and speech separate: it speaks `speech_text` when present, otherwise `display_text` rendered for speech per spec 090; rich documents are summarized aloud and offered on Telegram.

During bring-up `apps/speaker` may call the Gateway's OpenAI-compatible HTTP endpoint on loopback with a Gateway token. That path has no device identity, voice-authority policy or proactive delivery, so it is for development only and never the production surface.

## Identity and voice authority

Principal: each speaker is paired like an edge device, with a revocable, capability-scoped device key (spec 100, Security) recorded as `ExternalIdentity(provider="speaker", subject=<device_id>)`. The plugin takes the turn's `principal_id` from that binding: the owner in single-principal mode, or a household principal whose grants cover only shared household resources once several people use the home. Voice never selects or changes the principal.

Voice identification: speaker identification (voice accounts) yields `SpeakerContext.speaker_label`, a hint that may select preferences such as the music account, persona or reply style. It never selects or changes `principal_id`, grants, tool policy or confirmation requirements; it is not written to memory as identity, not persisted in task bindings, and not put into model context as a claim about who spoke. Unrecognized voices (guests, children) get household defaults.

Voice authority: a voice turn authorizes nothing sensitive (spec 020, Confirmation policy). Tools with risk `sensitive`, `destructive` or `external_write`, finance mutations and payments, lock/alarm/garage/door actions, and messages sent as the user never execute on voice alone. A spoken «да» on the speaker is not a confirmation: the action becomes an approval request on an authenticated surface (Telegram or web), and the speaker says where to approve it. TV audio, guests and replayed recordings can pass the wake chain, which confirms a phrase, not a person.

## Audio pipeline

1. Capture 6-channel 16 kHz from the ReSpeaker; ch0 is the primary stream, ch1-4 feed DOA/energy, ch5 is the reference for what the array itself played.
2. Front-end: frozen XVF tuning; media ducking on wake; later, reference-based echo cancellation for media played by this host (Kodi, Music Assistant) through PipeWire `module-echo-cancel`.
3. VAD (streaming, CPU): Silero VAD v6 as the default gate; FireRedVAD streaming is evaluated as a replacement for its lower false-alarm rate.
4. Wake stage 1 (CPU): a small detector tuned for recall, trained on this home's audio.
5. Wake stage 2 (about 300 ms on GPU, at most 800 ms on the CPU fallback):
   - ASR confirmation of the 2-3 s wake buffer with a phonetic fuzzy match of the phrase, always required;
   - context only raises thresholds and never rejects a wake by itself: a median DOA inside the configured TV sector, or media playing (state from Home Assistant / the local player), switches stage 1 and stage 2 to stricter thresholds;
   - speaker verification against enrolled household voices (ONNX embeddings, CPU) is a score for the stage-2 decision and for preferences; a voice that matches no enrollment (guest, child) is never rejected for that reason alone.
6. Utterance capture until the VAD end-of-speech decision (hangover about 700 ms, cap 15 s), then final STT.

Training data must match inference input. The production stream is ch0 as delivered by the XVF with frozen tuning, with no host-side noise suppression or AGC: the WebRTC NS/AGC that the legacy Wyoming satellite applies is removed from the wake/ASR path, because it measurably cut stage-2 recall and raised false accepts. Every model in the chain is trained and evaluated on that stream; when host media AEC is enabled, its output becomes the production stream and all models are re-evaluated on it.

## Wake phrase

The phrase is a configuration value, not code. A candidate is accepted by measurement, not by counting syllables or letters:
- its phonetic key has a low near-match rate in ASR transcripts of household audio (TV on and off) and in public Russian speech corpora, measured before any training;
- it has a stressed, clear vowel and is not a common word, name or brand heard on TV;
- filler prefixes (эй, окей) do not count toward distinctiveness, because they are frequent in speech and on TV.

Current phrases: «Брачо» for the primary persona and «Рэя» for a second, female persona. Each is screened and benchmarked separately, and both may be active at once; the phrase selects a persona (voice and style), never authority.

The first model (`ey_milosh`) failed exactly here: without the filler, «Милош» has a reduced unstressed vowel and a prefix shared with common words (мило, милый, миллион).

## Acceptance metrics

Every model or pipeline change is accepted only through benchmarks; numbers are recorded in the repository that owns the component.

Wake word:
- full two-stage chain: at most 1 false accept in at least 100 hours of household production-stream audio recorded with no wake phrase spoken, TV and music included (one-sided 95% upper bound at most 0.05 per hour);
- stage 1 alone at most 2 false accepts per hour on the same audio;
- false-reject rate at most 5% at that operating point on at least 300 real utterances from at least 3 enrolled household speakers at 1-4 m, with and without TV; reported separately, at most 10% each, for unenrolled speakers and for positions inside the TV sector (TV off and on);
- the benchmark must pass its own sanity gates: no silent audio, non-constant model output, and a replay test that reproduces the logged production detections at their exact times on collection-session audio from the same period.

VAD: frame-level false-alarm rate and F1 on a labeled household set (speech, TV speech, music, silence), plus FLEURS-VAD and ESC-50 as public references.

STT: WER on at least 300 household commands captured from the production stream (TV off and on) and on Golos farfield/crowd. Latency is measured from the VAD end-of-speech decision (true speech end precedes it by the hangover): final text p50 at most 300 ms and p95 at most 700 ms on the GPU, p95 at most 1.5 s on the CPU fallback.

End to end: wake-to-first-audio latency and task success on a scripted command set.

## Model choices (September 2026 survey)

- Wake stage 1: LiveKit wakeword and openWakeWord are compared on `wakebench`; microWakeWord is a fallback.
- STT: GigaAM-v3 (MIT) is the primary Russian engine; Whisper large-v3-turbo stays as a second opinion and for long-form; Vosk small-streaming on CPU gives partials.
- Speaker verification: WeSpeaker ResNet34 or CAM++ via ONNX.
- TTS runs locally on the home server with a designed voice per persona, not a clone of a real person; the engine is chosen by benchmark in rollout step 4.
- Training runs on the home server GPU (see Privacy and retention).

## Integration with Home Assistant

During migration `vad-ctl` is the Wyoming satellite for HA Assist: it owns the only ReSpeaker capture, runs its own wake chain and streams the post-wake utterance to HA, and the legacy `wyoming-satellite` and separate wake service are stopped. Wyoming is unauthenticated, so the endpoint binds to 127.0.0.1 or a Tailnet-ACL'd address, and the local event socket is mode 0600 or group-restricted.

`wyoming-satellite` is archived upstream (January 2026); the long-term HA path is `linux-voice-assistant` or `apps/speaker` itself.

## Privacy and retention

- Always-on audio is processed in RAM. The pre-wake ring buffer holds at most 30 s and is never written to disk outside a collection session.
- Production logs keep timestamps, scores, DOA and media state per detection, not audio. The wake buffer (at most 3 s) of each accepted wake is kept 7 days for false-wake review, then deleted. Command audio is discarded after STT.
- Raw audio reaches disk only in an explicit collection session: started by a household member, shown by a visible indicator, announced to the household, stopped automatically after at most 7 days, encrypted at rest. Session audio is deleted 30 days after the session ends, except segments a household member promotes into the labeled benchmark/training set; those stay listed, encrypted and individually deletable.
- Training and evaluation on household audio run on the home server GPU. Household audio, and features or embeddings derived from it, never leave the house; a rented GPU may run only jobs whose inputs are synthetic or public data.
- Voiceprints are biometric data. Enrollment is opt-in per person (a parent decides for a child); enrollment audio is deleted once embeddings are computed; embeddings stay on the home server, encrypted at rest, never reach OpenClaw memory or any cloud service, and each person can delete theirs at any time.
- Only the final transcript of an accepted utterance leaves the speaker, to the OpenClaw Gateway. From there it may reach a cloud model provider per spec 040, including transcripts of false wakes that passed stage 2. Audio, voiceprints, DOA and `speaker_label` are never sent to model providers. A hardware mute stops capture, and the owner can pin speaker turns to local models through the spec 040 alias mapping.

## Deployment

The speaker runs on the home server inside the Tailnet and is paired with the Gateway like an ORC edge device (spec 100, Security): outbound connections only, a revocable capability-scoped device key, and secrets that stay on the device. Hostnames, addresses and device inventory are deployment configuration, not repository content (spec 000 §19).

## Rollout

1. Honest benchmark and hard-negative mining on real household audio (`wakebench`).
2. «Брачо», then «Рэя»: synthetic plus real positives, stage 1 model, stage 2 verifier; ship behind the Wyoming endpoint.
3. `stt-lib` on Linux/CUDA with GigaAM-v3; streaming utterance API.
4. `apps/speaker` and `openclaw-speaker` end to end with pairing, TTS, ducking and follow-ups.
5. Front-end improvements: media reference AEC, TV-sector thresholds, speaker enrollment UI.

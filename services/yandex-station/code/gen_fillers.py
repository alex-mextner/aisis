#!/usr/bin/env python3
"""Regenerate filler audio per voice (idempotent).

Reads fillers.json (repo texts), synthesizes each phrase through the local
adapter (http://127.0.0.1:31117/v1/audio/speech, OpenAI audio-speech API shape)
pinned to one voice with X-AISIS-Voice, and caches ogg files per voice in
~/.cache/aisis/fillers/<voice>/<category>_<n>.ogg. The top-level
index.json always points at the ACTIVE voice (files are referenced as
"<voice>/<file>"), so switching the voice in the dashboard only needs this
script to run once per voice; audio is kept while its phrase in fillers.json is
unchanged and resynthesized when the phrase changed (--force redoes everything).

  gen_fillers.py            active voice from the dashboard setting
  gen_fillers.py --voice alice|bracho
  gen_fillers.py --force

Voice "alice" is Yandex cloud (keyless Alice uniproxy by default, SpeechKit when
configured): its phrases are sent to Yandex once. Each voice dir records the engine,
voice and emotion it was synthesized with (.spec), so changing any of them resynthesizes.
No playback here: the speaker is never touched by this script.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import urllib.request
from pathlib import Path

BASE = Path.home() / '.local' / 'share' / 'aisis'
sys.path.insert(0, str(BASE))
import tts_settings  # noqa: E402

OUT = Path.home() / '.cache' / 'aisis' / 'fillers'
TTS_URL = os.environ.get('AISIS_TTS_URL', 'http://127.0.0.1:31117/v1/audio/speech')
MODEL = os.environ.get('TTS_OPENAI_MODEL', 'bracho')
VOICE = os.environ.get('TTS_OPENAI_VOICE', 'Брачо')


def synth(text: str, voice: str) -> bytes:
    body = json.dumps({'model': MODEL, 'voice': VOICE, 'input': text,
                       'response_format': 'opus'}).encode()
    req = urllib.request.Request(TTS_URL, data=body, method='POST')
    req.add_header('Content-Type', 'application/json')
    req.add_header('X-AISIS-Voice', voice)
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read()


def adopt_legacy_bracho() -> None:
    """Pre-per-voice fillers sat in the top-level dir and were Bracho: copy them (the
    running container still reads those names, so the originals stay)."""
    target = OUT / 'bracho'
    legacy = sorted(OUT.glob('*.ogg'))
    if not legacy or target.exists():
        return
    target.mkdir()
    for path in legacy:
        shutil.copy2(path, target / path.name)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--voice', choices=tts_settings.VOICES)
    parser.add_argument('--force', action='store_true')
    args = parser.parse_args()
    voice = args.voice or tts_settings.effective_voice()
    cfg = json.loads((BASE / 'fillers.json').read_text())
    OUT.mkdir(parents=True, exist_ok=True)
    adopt_legacy_bracho()
    vdir = OUT / voice
    vdir.mkdir(exist_ok=True)
    spec = tts_settings.voice_spec(voice=voice)
    spec_file = vdir / '.spec'
    try:
        recorded = spec_file.read_text()
    except OSError:
        recorded = None
    # Legacy bracho files predate .spec and stay valid; any other voice without a record is of unknown origin.
    stale = recorded != spec and not (recorded is None and voice == 'bracho')
    index: dict[str, list[dict]] = {}
    try:
        previous = json.loads((vdir / 'index.json').read_text())
    except (OSError, ValueError):
        previous = {}
    for category, phrases in cfg['categories'].items():
        index[category] = []
        spoken_before = {entry['file']: entry['text'] for entry in previous.get(category, [])}
        for i, text in enumerate(phrases):
            path = vdir / f'{category}_{i:02d}.ogg'
            file = f'{voice}/{path.name}'
            # Keep audio only while the phrase it was synthesized from is unchanged.
            if args.force or stale or spoken_before.get(file) != text or not path.exists() or path.stat().st_size == 0:
                audio = synth(text, voice)
                tmp = path.with_suffix('.tmp')
                tmp.write_bytes(audio)
                os.replace(tmp, path)
                print(f'synthesized {voice}/{path.name}: {len(audio)} bytes')
            else:
                print(f'kept {voice}/{path.name}: {path.stat().st_size} bytes')
            index[category].append({'text': text, 'file': f'{voice}/{path.name}'})
    spec_file.write_text(spec)
    payload = json.dumps(index, ensure_ascii=False, indent=1)
    (vdir / 'index.json').write_text(payload)
    tmp = OUT / 'index.json.tmp'
    tmp.write_text(payload)
    os.replace(tmp, OUT / 'index.json')
    print(f'index.json -> voice {voice}, {sum(len(v) for v in index.values())} fillers')
    return 0


if __name__ == '__main__':
    sys.exit(main())

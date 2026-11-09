#!/usr/bin/env python3
"""Nightly ASR candidate benchmark over the always-on voice archive (voiceclips.py).

Re-transcribes every archived clip with each candidate engine on the CPU, stores one row per
(clip, engine) in `asr_bench` inside the dialogs DB and answers `summary()` for the dashboard
(`/api/asr/bench`). Nothing leaves the machine; engines load one at a time and are dropped
before the next so memory stays at a single model.

What is measured, per engine:
  * text_on_gated    non-empty text on clips the STT gate dropped before the recognizer
                     (silence/room noise/TV) or that a human marked "no speech": the
                     silence-hallucination metric. Gated clips can hide quiet real speech, so
                     a hit is a review item, not a proof; a human reference of "" settles it.
  * pattern / loops  known subtitle-credit captions (stt_wyoming.is_silence_hallucination) and
                     repeated-token loops on any clip.
  * latency          p50/p95 wall ms and RTFx (audio seconds per compute second, 2 threads).
  * wer_ref          WER against human-verified text in `asr_reference` (the only ground truth;
                     the live transcript is Whisper's and is NOT treated as truth).
  * wer_live         disagreement with the live (Whisper) transcript, and `pairwise` between
                     engines. High disagreement flags clips to review, it does not rank engines.

Every engine gets the same lifted copy of the clip (see lift()), as the live gateway does; the archive keeps
the original quiet audio. Engines are plain names in ENGINES (ONNX Runtime via onnx-asr, or faster-whisper) plus any
running Wyoming recognizer declared as `AISIS_ASR_BENCH_WYOMING=name=tcp://host:port,...`, so a
Qwen3-ASR or Nemotron Wyoming server is benchmarked unchanged. Use non-e2e GigaAM variants:
the e2e ones write digits and punctuation, which inflates WER against spelled-out references.

The run is polite by design: it renices itself to 19, uses 2 threads, exits at once when the
1-minute load average is above AISIS_ASR_BENCH_MAX_LOAD (default half the cores), stops at
--budget-s, and only does clip/engine pairs that have no row yet.

    bench_asr_archive.py run [--engines a,b] [--max-clips N] [--budget-s S]
    bench_asr_archive.py summary
    bench_asr_archive.py reference CLIP_ID "verified text"      # "" marks a clip as no speech
"""
from __future__ import annotations

import argparse
import asyncio
import fcntl
import gc
import json
import math
import os
import re
import sqlite3
import subprocess
import sys
import time
from typing import Callable

THREADS = int(os.environ.get("AISIS_ASR_BENCH_THREADS", "2"))
for _var in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ.setdefault(_var, str(THREADS))

import voiceclips  # noqa: E402  (after the thread caps so numpy/ORT inherit them)

RATE = 16000
DEFAULT_ENGINES = ("giga-v3-ctc", "giga-v3-rnnt", "tone", "parakeet-v3")
# name -> (loader kind, model id, quantization)
ENGINES = {
    "giga-v3-ctc": ("onnx", "gigaam-v3-ctc", "int8"),
    "giga-v3-rnnt": ("onnx", "gigaam-v3-rnnt", "int8"),
    "giga-multi-ctc": ("onnx", "gigaam-multilingual-ctc", "int8"),
    "tone": ("onnx", "t-tech/t-one", None),
    "parakeet-v3": ("onnx", "nemo-parakeet-tdt-0.6b-v3", "int8"),
    "whisper-turbo": ("fw", "mobiuslabsgmbh/faster-whisper-large-v3-turbo", "int8"),
}
LIVE = "live"  # the transcript the gateway stored when the clip was recorded (Whisper today)

SCHEMA = (
    "CREATE TABLE IF NOT EXISTS asr_bench("
    "clip_id TEXT NOT NULL, engine TEXT NOT NULL, text TEXT, ms INTEGER, dur REAL, empty INTEGER, "
    "pattern INTEGER, loop INTEGER, err TEXT, ts REAL NOT NULL, PRIMARY KEY(clip_id, engine))",
    "CREATE INDEX IF NOT EXISTS asr_bench_engine ON asr_bench(engine)",
    "CREATE TABLE IF NOT EXISTS asr_reference(clip_id TEXT PRIMARY KEY, text TEXT NOT NULL, source TEXT, ts REAL NOT NULL)",
    "CREATE TABLE IF NOT EXISTS asr_bench_runs(ts REAL NOT NULL, engines TEXT, done INTEGER, skipped TEXT, seconds REAL)",
)

# Decision criteria. Mirrored in docs/asr-landscape-2026-11.md; the summary applies them.
CRITERIA = {
    "min_gated_clips": 50,        # fewer gated clips than this and the hallucination verdict is "insufficient"
    "max_text_on_gated": 0,       # per engine, after human review of the hits
    "max_pattern": 0,
    "max_loops": 0,
    "min_reference_clips": 30,    # below this WER vs reference is "insufficient"
    "max_wer_ref_vs_best": 0.02,  # absolute WER distance allowed to the best engine
    "max_p95_ms": 1000,
}

_LOOP = re.compile(r"\b(\w+)(?:\s+\1\b){3,}", re.IGNORECASE)


def norm(text: str) -> list[str]:
    text = (text or "").lower().replace("ё", "е")
    return re.sub(r"[^\w\s]", " ", text).split()


def edit_distance(a: list[str], b: list[str]) -> int:
    prev = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        cur = [i]
        for j, y in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (x != y)))
        prev = cur
    return prev[-1]


def wer(ref: str, hyp: str) -> float:
    r, h = norm(ref), norm(hyp)
    if not r:
        return 0.0 if not h else float(len(h))
    return edit_distance(r, h) / len(r)


def pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, math.ceil(q * len(ordered)) - 1)]


def connect(db_path: str) -> sqlite3.Connection:
    conn = voiceclips.connect(db_path)
    for statement in SCHEMA:
        conn.execute(statement)
    return conn


def decode(path: str) -> "np.ndarray":
    import numpy as np
    ffmpeg = voiceclips._ffmpeg()
    if not ffmpeg:
        raise RuntimeError("ffmpeg_missing")
    raw = subprocess.run([ffmpeg, "-v", "error", "-i", path, "-f", "s16le", "-ac", "1", "-ar", str(RATE), "-"],
                         check=True, capture_output=True, timeout=60).stdout
    return np.frombuffer(raw, dtype="<i2").astype("float32") / 32768.0


def lift(wave: "np.ndarray", voiced_ms: int | None) -> "np.ndarray":
    """The recognizer copy the live gateway feeds (stt_wyoming.lift_for_recognizer): archived clips keep the
    station's original ~-45 dBFS level, and fixed-floor mel front-ends (GigaAM, onnx-asr) and Whisper both
    do worse on it. The archive stores voiced_ms but not the voiced RMS, so the RMS is taken over the loudest
    voiced_ms/20 frames. AISIS_ASR_BENCH_LIFT=0 feeds the original audio."""
    if os.environ.get("AISIS_ASR_BENCH_LIFT", "1") == "0":
        return wave
    import numpy as np
    from stt_wyoming import FRAME_MS, Tuning, lift_for_recognizer
    pcm = (wave * 32768).clip(-32768, 32767).astype("<i2")
    frame = RATE * FRAME_MS // 1000
    count = len(pcm) // frame
    if count == 0:
        return wave
    energy = (pcm[:count * frame].astype("float64").reshape(count, frame) ** 2).mean(axis=1)
    top = max(1, min(count, (voiced_ms or 0) // FRAME_MS or count // 3))
    voiced_rms = float(np.sqrt(np.sort(energy)[-top:].mean()))
    lifted, _ = lift_for_recognizer(pcm.tobytes(), voiced_rms, Tuning.from_env(os.environ))
    return np.frombuffer(lifted, dtype="<i2").astype("float32") / 32768.0


def load_onnx(model_id: str, quantization: str | None) -> Callable[["np.ndarray"], str]:
    import onnx_asr
    import onnxruntime as ort
    options = ort.SessionOptions()
    options.intra_op_num_threads = THREADS
    options.inter_op_num_threads = 1
    kwargs = {"sess_options": options}
    if quantization:
        kwargs["quantization"] = quantization
    model = onnx_asr.load_model(model_id, **kwargs)
    return lambda wave: model.recognize(wave, sample_rate=RATE)


def load_faster_whisper(model_id: str, compute_type: str | None) -> Callable[["np.ndarray"], str]:
    from faster_whisper import WhisperModel
    model = WhisperModel(model_id, device="cpu", compute_type=compute_type or "int8", cpu_threads=THREADS)

    def run(wave):
        segments, _ = model.transcribe(wave, language="ru", beam_size=1, condition_on_previous_text=False)
        return " ".join(s.text.strip() for s in segments)
    return run


def wyoming_engines() -> dict[str, str]:
    out = {}
    for item in filter(None, os.environ.get("AISIS_ASR_BENCH_WYOMING", "").split(",")):
        name, _, uri = item.partition("=")
        if name.strip() and uri.strip():
            out[name.strip()] = uri.strip()
    return out


def load_wyoming(uri: str) -> Callable[["np.ndarray"], str]:
    from stt_wyoming import transcribe

    def run(wave):
        pcm = (wave * 32767).clip(-32768, 32767).astype("<i2").tobytes()
        return asyncio.run(transcribe(uri, pcm, RATE, "ru", timeout=60))
    return run


def load_engine(name: str) -> Callable[["np.ndarray"], str]:
    remote = wyoming_engines()
    if name in remote:
        return load_wyoming(remote[name])
    if name not in ENGINES:
        raise KeyError(f"unknown engine {name!r}; known: {', '.join(sorted(ENGINES) + sorted(remote))}")
    kind, model_id, quantization = ENGINES[name]
    return load_onnx(model_id, quantization) if kind == "onnx" else load_faster_whisper(model_id, quantization)


def flags(text: str) -> tuple[int, int, int]:
    from stt_wyoming import is_silence_hallucination
    stripped = (text or "").strip()
    return (int(not stripped), int(bool(stripped) and is_silence_hallucination(stripped)),
            int(bool(_LOOP.search(stripped))))


def load_too_high() -> float | None:
    limit = float(os.environ.get("AISIS_ASR_BENCH_MAX_LOAD", (os.cpu_count() or 4) / 2))
    load = os.getloadavg()[0]
    return load if load > limit else None


def pending(conn: sqlite3.Connection, engine: str, limit: int) -> list[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    return conn.execute(
        "SELECT c.id, c.path, c.dur, c.voiced_ms FROM voice_clips c LEFT JOIN asr_bench b ON b.clip_id=c.id AND b.engine=? "
        "WHERE b.clip_id IS NULL ORDER BY c.ts DESC LIMIT ?", (engine, limit)).fetchall()


def run(db_path: str, engines: list[str], max_clips: int, budget_s: float) -> dict:
    started = time.time()
    lock = open(db_path + ".asrbench.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return {"skipped": "another run holds the lock", "done": 0}
    try:
        busy = load_too_high()
        conn = connect(db_path)
        if busy is not None:
            reason = f"load {busy:.1f} above limit"
            conn.execute("INSERT INTO asr_bench_runs VALUES(?,?,?,?,?)", (started, ",".join(engines), 0, reason, 0.0))
            conn.commit()
            return {"skipped": reason, "done": 0}
        done = 0
        for name in engines:
            if time.time() - started > budget_s:
                break
            rows = pending(conn, name, max_clips)
            if not rows:
                continue
            try:
                recognize = load_engine(name)
            except Exception as exc:  # a missing package or model must not stop the other engines
                print(f"engine {name} unavailable: {exc}", file=sys.stderr)
                continue
            for row in rows:
                if time.time() - started > budget_s or load_too_high() is not None:
                    break
                clip_path = voiceclips.root() / row["path"]
                text, err, ms, dur = "", None, 0, row["dur"] or 0.0
                try:
                    wave = decode(str(clip_path))
                    dur = len(wave) / RATE
                    wave = lift(wave, row["voiced_ms"])
                    t0 = time.perf_counter()
                    text = recognize(wave).strip()
                    ms = int((time.perf_counter() - t0) * 1000)
                except Exception as exc:
                    err = str(exc)[:200]
                empty, pattern, loop = flags(text)
                conn.execute("INSERT OR REPLACE INTO asr_bench VALUES(?,?,?,?,?,?,?,?,?,?)",
                             (row["id"], name, text, ms, dur, empty, pattern, loop, err, time.time()))
                conn.commit()
                done += 1
            del recognize
            gc.collect()
        conn.execute("INSERT INTO asr_bench_runs VALUES(?,?,?,?,?)",
                     (started, ",".join(engines), done, None, time.time() - started))
        conn.commit()
        return {"done": done, "seconds": round(time.time() - started, 1)}
    finally:
        lock.close()


def set_reference(db_path: str, clip_id: str, text: str, source: str = "human") -> None:
    conn = connect(db_path)
    conn.execute("INSERT OR REPLACE INTO asr_reference VALUES(?,?,?,?)", (clip_id, text.strip(), source, time.time()))
    conn.commit()
    conn.close()


def summary(db_path: str) -> dict:
    conn = connect(db_path)
    conn.row_factory = sqlite3.Row
    clips = {r["id"]: r for r in conn.execute("SELECT id, status, transcript, voiced_ms FROM voice_clips")}
    refs = {r["clip_id"]: r["text"] for r in conn.execute("SELECT clip_id, text FROM asr_reference")}
    rows = conn.execute("SELECT clip_id, engine, text, ms, dur, empty, pattern, loop, err FROM asr_bench").fetchall()
    last = conn.execute("SELECT ts, engines, done, skipped, seconds FROM asr_bench_runs ORDER BY ts DESC LIMIT 1").fetchone()
    conn.close()

    def gated(clip_id: str) -> bool:
        if clip_id in refs:
            return refs[clip_id] == ""  # a human verdict beats the gate
        return clip_id in clips and clips[clip_id]["status"] != "sent"

    by_engine: dict[str, dict[str, str]] = {}
    stats: dict[str, dict] = {}
    for r in rows:
        by_engine.setdefault(r["engine"], {})[r["clip_id"]] = r["text"] or ""
        s = stats.setdefault(r["engine"], {"n": 0, "errors": 0, "empty": 0, "pattern": 0, "loops": 0, "gated_clips": 0,
                                           "text_on_gated": 0, "ms": [], "audio_s": 0.0, "ref": [], "live": []})
        if r["err"]:
            s["errors"] += 1
            continue
        s["n"] += 1
        s["empty"] += r["empty"]
        s["pattern"] += r["pattern"]
        s["loops"] += r["loop"]
        s["ms"].append(r["ms"])
        s["audio_s"] += r["dur"] or 0.0
        if gated(r["clip_id"]):
            s["gated_clips"] += 1
            s["text_on_gated"] += 0 if r["empty"] else 1
        if r["clip_id"] in refs and refs[r["clip_id"]]:
            s["ref"].append(wer(refs[r["clip_id"]], r["text"] or ""))
        live = (clips[r["clip_id"]]["transcript"] if r["clip_id"] in clips else None) or ""
        if live and r["text"]:
            s["live"].append(wer(live, r["text"]))

    engines = {}
    for name, s in stats.items():
        total_ms = sum(s["ms"])
        engines[name] = {
            "n": s["n"], "errors": s["errors"], "empty_rate": round(s["empty"] / s["n"], 4) if s["n"] else None,
            "gated_clips": s["gated_clips"], "text_on_gated": s["text_on_gated"], "pattern": s["pattern"], "loops": s["loops"],
            "p50_ms": pct(s["ms"], 0.5), "p95_ms": pct(s["ms"], 0.95),
            "rtfx": round(s["audio_s"] * 1000 / total_ms, 1) if total_ms else None,
            "ref_clips": len(s["ref"]), "wer_ref": round(sum(s["ref"]) / len(s["ref"]), 4) if s["ref"] else None,
            "live_clips": len(s["live"]), "wer_live": round(sum(s["live"]) / len(s["live"]), 4) if s["live"] else None,
        }
    best = min((e["wer_ref"] for e in engines.values() if e["wer_ref"] is not None and e["ref_clips"] >= CRITERIA["min_reference_clips"]), default=None)
    for e in engines.values():
        why = []
        if e["gated_clips"] < CRITERIA["min_gated_clips"]:
            why.append("insufficient gated clips")
        elif e["text_on_gated"] > CRITERIA["max_text_on_gated"]:
            why.append("text on gated clips")
        if e["pattern"] > CRITERIA["max_pattern"]:
            why.append("subtitle-credit captions")
        if e["loops"] > CRITERIA["max_loops"]:
            why.append("repetition loops")
        if e["p95_ms"] is not None and e["p95_ms"] > CRITERIA["max_p95_ms"]:
            why.append("p95 latency")
        if e["ref_clips"] < CRITERIA["min_reference_clips"]:
            why.append("insufficient references")
        elif best is not None and e["wer_ref"] - best > CRITERIA["max_wer_ref_vs_best"]:
            why.append("WER vs best")
        e["eligible"] = not why
        e["blockers"] = why

    pairwise = {}
    names = sorted(by_engine)
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            common = [c for c in by_engine[a] if c in by_engine[b] and by_engine[a][c] and by_engine[b][c]]
            if common:
                pairwise[f"{a}|{b}"] = {"clips": len(common),
                                        "wer": round(sum(wer(by_engine[a][c], by_engine[b][c]) for c in common) / len(common), 4)}
    return {
        "clips": len(clips), "sent": sum(1 for c in clips.values() if c["status"] == "sent"),
        "gated": sum(1 for c in clips.values() if c["status"] != "sent"), "references": len(refs),
        "engines": engines, "pairwise": pairwise, "criteria": CRITERIA,
        "last_run": dict(last) if last else None,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_run = sub.add_parser("run")
    p_run.add_argument("--engines", default=os.environ.get("AISIS_ASR_BENCH_ENGINES", ",".join(DEFAULT_ENGINES)))
    p_run.add_argument("--max-clips", type=int, default=int(os.environ.get("AISIS_ASR_BENCH_MAX_CLIPS", "400")))
    p_run.add_argument("--budget-s", type=float, default=float(os.environ.get("AISIS_ASR_BENCH_BUDGET_S", "3000")))
    sub.add_parser("summary")
    p_ref = sub.add_parser("reference")
    p_ref.add_argument("clip_id")
    p_ref.add_argument("text")
    args = parser.parse_args(argv)
    import dialogstore
    db = dialogstore.DB_PATH
    if args.cmd == "run":
        try:
            os.nice(19)
        except OSError:
            pass
        engines = [e.strip() for e in args.engines.split(",") if e.strip()]
        print(json.dumps(run(db, engines, args.max_clips, args.budget_s), ensure_ascii=False))
    elif args.cmd == "summary":
        print(json.dumps(summary(db), ensure_ascii=False, indent=1))
    else:
        set_reference(db, args.clip_id, args.text)
    return 0


if __name__ == "__main__":
    sys.exit(main())

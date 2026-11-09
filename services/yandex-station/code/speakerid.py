#!/usr/bin/env python3
"""Speaker verification for the Yandex STT gateway: "is this the owner's voice?".

Embeds each utterance with an ONNX speaker model (WeSpeaker/3D-Speaker family,
80-dim Kaldi fbank input) and compares it by cosine to enrolled voiceprints.
numpy and onnxruntime are imported lazily so `import speakerid` always works;
every failure degrades to verdict "unknown" at the caller.

Audio is never stored unless the opt-in `capture` setting is on; then at most
`capture_max` utterances younger than `capture_days` days are kept as 16 kHz wav
in the voiceprint state dir so the owner can enrol from them.
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import secrets
import sqlite3
import sys
import tempfile
import threading
import time
import wave
from pathlib import Path

LOG = logging.getLogger("aisis.speakerid")

MODES = ("off", "shadow", "enforce")
# Cosine at ~1% false-accept on foreign/TV speech with the default model
# (see RESULTS.md); re-tune from the dashboard after collecting real scores.
DEFAULT_THRESHOLD = 0.46
DEFAULTS = {"mode": "shadow", "threshold": DEFAULT_THRESHOLD, "capture": False, "capture_max": 60, "capture_days": 14}
DEFAULT_MODEL_FILE = "wespeaker_en_voxceleb_CAM++.onnx"
MODEL_RATE = 16000
MIN_SECONDS = 1.0
MAX_SECONDS = 15.0
_ID_RE = re.compile(r"^[0-9]{8}T[0-9]{6}-[0-9a-f]{6}$")
_NAME_RE = re.compile(r"^[^\x00-\x1f/\\]{1,64}$")


# ---------------------------------------------------------------- paths

def state_dir() -> Path:
    return Path(os.environ.get("AISIS_VOICEPRINT_DIR", str(Path.home() / ".local" / "state" / "aisis" / "voiceprint")))


def samples_dir() -> Path:
    return state_dir() / "samples"


def profiles_path() -> Path:
    return state_dir() / "profiles.json"


def settings_path() -> Path:
    default = Path.home() / ".config" / "aisis" / "voiceprint.json"
    return Path(os.environ.get("AISIS_VOICEPRINT_SETTINGS", str(default)))


def model_path() -> Path:
    env = os.environ.get("AISIS_SPEAKER_MODEL")
    if env:
        return Path(env)
    return Path.home() / ".local" / "share" / "aisis" / "models" / DEFAULT_MODEL_FILE


def _write_private(path: Path, data: bytes) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


# ---------------------------------------------------------------- settings

def _validate(settings: dict) -> dict:
    if settings["mode"] not in MODES:
        raise ValueError("mode must be one of " + "|".join(MODES))
    threshold = settings["threshold"]
    if isinstance(threshold, bool) or not isinstance(threshold, (int, float)) or not -1.0 <= threshold <= 1.0:
        raise ValueError("threshold must be a number in [-1, 1]")
    settings["threshold"] = float(threshold)
    if not isinstance(settings["capture"], bool):
        raise ValueError("capture must be boolean")
    for key, low, high in (("capture_max", 1, 1000), ("capture_days", 1, 365)):
        value = settings[key]
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise ValueError("%s must be an integer in [%d, %d]" % (key, low, high))
    return settings


def load_settings() -> dict:
    """Settings with defaults; a missing/corrupt file or invalid value falls back to the default."""
    settings = dict(DEFAULTS)
    try:
        stored = json.loads(settings_path().read_text())
    except (OSError, ValueError):
        return settings
    if not isinstance(stored, dict):
        return settings
    for key in DEFAULTS:
        if key in stored:
            candidate = dict(settings)
            candidate[key] = stored[key]
            try:
                settings = _validate(candidate)
            except ValueError:
                pass
    return settings


def update_settings(changes: dict) -> dict:
    """Merge known keys into the stored settings (validated, atomic, 0600)."""
    settings = load_settings()
    for key, value in changes.items():
        if key not in DEFAULTS:
            raise ValueError("unknown setting " + str(key))
        settings[key] = value
    _validate(settings)
    _write_private(settings_path(), (json.dumps(settings, indent=1) + "\n").encode())
    return settings


def status() -> dict:
    """Public view for the dashboard."""
    path = model_path()
    try:
        import onnxruntime  # noqa: F401
        import numpy  # noqa: F401
        runtime = True
    except ImportError:
        runtime = False
    return {"settings": load_settings(), "model": path.stem, "model_present": path.is_file(), "runtime_available": runtime,
            "profiles": profiles_summary(), "samples": len(list_samples()), "modes": list(MODES)}


# ---------------------------------------------------------------- audio / features

def _np():
    import numpy
    return numpy


def pcm_to_float(pcm: bytes):
    np = _np()
    return np.frombuffer(pcm, dtype="<i2", count=len(pcm) // 2).astype(np.float32) / 32768.0


def resample(x, rate: int, target: int = MODEL_RATE):
    """Polyphase-style FIR resampler (Kaiser lowpass), numpy only."""
    np = _np()
    if rate == target:
        return x
    g = math.gcd(rate, target)
    up, down = target // g, rate // g
    if up > 1:
        stuffed = np.zeros(len(x) * up, dtype=np.float32)
        stuffed[::up] = x * up
        x = stuffed
    cutoff = 0.5 / max(up, down)  # cycles per input sample of the (upsampled) signal
    half = 16 * max(up, down)
    t = np.arange(-half, half + 1, dtype=np.float64)
    taps = (2 * cutoff * np.sinc(2 * cutoff * t) * np.kaiser(2 * half + 1, 8.0)).astype(np.float32)
    padded = np.pad(x, (half, half))
    n_out = (len(x) + down - 1) // down
    # Evaluate the FIR only at the kept output positions.
    idx = (np.arange(n_out)[:, None] * down) + np.arange(2 * half + 1)[None, :]
    needed = int(idx[-1, -1]) + 1 if n_out else 0
    if needed > len(padded):
        padded = np.pad(padded, (0, needed - len(padded)))
    return (padded[idx] @ taps[::-1]).astype(np.float32)


_MEL_CACHE: dict = {}


def _mel_bank(num_mel: int = 80, n_fft: int = 512, rate: int = MODEL_RATE, low: float = 20.0):
    key = (num_mel, n_fft, rate, low)
    bank = _MEL_CACHE.get(key)
    if bank is not None:
        return bank
    np = _np()
    mel = lambda f: 1127.0 * np.log(1.0 + f / 700.0)
    bins = n_fft // 2
    width = rate / n_fft
    mel_low, mel_high = mel(low), mel(rate / 2.0)
    delta = (mel_high - mel_low) / (num_mel + 1)
    m = mel(width * np.arange(bins))
    out = np.zeros((num_mel, bins + 1), dtype=np.float32)
    for b in range(num_mel):
        left = mel_low + b * delta
        center = left + delta
        right = center + delta
        out[b, :bins] = np.maximum(0.0, np.minimum((m - left) / (center - left), (right - m) / (right - center)))
    bank = out.T.copy()
    _MEL_CACHE[key] = bank
    return bank


def fbank(wave_float, scale: float):
    """Kaldi compute-fbank-feats (25 ms/10 ms, povey window, preemph .97, 80 mel, no dither) + per-utterance mean norm."""
    np = _np()
    x = wave_float.astype(np.float32) * scale
    frame, shift, n_fft = 400, 160, 512
    if len(x) < frame:
        return None
    n = 1 + (len(x) - frame) // shift
    frames = np.lib.stride_tricks.as_strided(x, shape=(n, frame), strides=(x.strides[0] * shift, x.strides[0])).copy()
    frames -= frames.mean(axis=1, keepdims=True)
    prev = np.concatenate([frames[:, :1], frames[:, :-1]], axis=1)
    frames -= 0.97 * prev
    window = (0.5 - 0.5 * np.cos(2 * np.pi * np.arange(frame) / (frame - 1))) ** 0.85
    frames *= window.astype(np.float32)
    power = np.abs(np.fft.rfft(frames, n=n_fft, axis=1)) ** 2
    feats = np.log(np.maximum(power @ _mel_bank(), np.finfo(np.float32).eps)).astype(np.float32)
    return feats - feats.mean(axis=0, keepdims=True)


class OnnxEmbedder:
    """WeSpeaker / 3D-Speaker style ONNX model: (1, T, 80) fbank -> (1, D) embedding."""

    def __init__(self, path: Path, threads: int):
        import onnxruntime
        opts = onnxruntime.SessionOptions()
        opts.intra_op_num_threads = threads
        opts.inter_op_num_threads = 1
        self.session = onnxruntime.InferenceSession(str(path), opts, providers=["CPUExecutionProvider"])
        meta = self.session.get_modelmeta().custom_metadata_map
        self.scale = 1.0 if meta.get("normalize_samples") == "1" else 32768.0
        self.input = self.session.get_inputs()[0].name

    def __call__(self, wave_float):
        feats = fbank(wave_float, self.scale)
        if feats is None:
            return None
        return self.session.run(None, {self.input: feats[None]})[0][0]


# ---------------------------------------------------------------- profiles

def _load_profile_file() -> dict:
    try:
        data = json.loads(profiles_path().read_text())
    except (OSError, ValueError):
        return {"version": 1, "model": "", "profiles": {}}
    if not isinstance(data, dict) or not isinstance(data.get("profiles"), dict):
        return {"version": 1, "model": "", "profiles": {}}
    return data


def _save_profile_file(data: dict) -> None:
    _write_private(profiles_path(), json.dumps(data).encode())


def profiles_summary() -> list[dict]:
    data = _load_profile_file()
    return [{"name": name, "embeddings": len(p.get("embeddings", [])), "created": p.get("created"), "model": data.get("model", "")}
            for name, p in sorted(data["profiles"].items())]


_PROFILE_LOCK = threading.Lock()


def delete_profile(name: str) -> bool:
    with _PROFILE_LOCK:
        data = _load_profile_file()
        if name not in data["profiles"]:
            return False
        del data["profiles"][name]
        _save_profile_file(data)
        return True


# ---------------------------------------------------------------- verifier

class Verifier:
    """Thread-safe embedding + scoring. `embedder` (wave_float32_16k -> vector) replaces the ONNX model, for tests."""

    def __init__(self, model: str | os.PathLike | None = None, embedder=None, model_tag: str | None = None,
                 threshold: float | None = None, threads: int | None = None):
        self._path = Path(model) if model else model_path()
        self._embedder = embedder
        self.model = model_tag or (self._path.stem if embedder is None else "stub")
        self._threshold = threshold
        self._threads = threads or int(os.environ.get("AISIS_SPEAKER_THREADS", "2"))
        self._load_lock = threading.Lock()
        self._profiles_lock = threading.Lock()
        self._profiles_stamp = None
        self._profiles: dict = {}

    @property
    def threshold(self) -> float:
        return self._threshold if self._threshold is not None else load_settings()["threshold"]

    def _model(self):
        if self._embedder is None:
            with self._load_lock:
                if self._embedder is None:
                    embedder = OnnxEmbedder(self._path, self._threads)
                    embedder(_np().zeros(MODEL_RATE, dtype="float32") + 1e-3)  # warm-up
                    self._embedder = embedder
        return self._embedder

    def embed(self, pcm: bytes, rate: int):
        """L2-normalised embedding of int16 mono LE PCM, or None when shorter than 1 s."""
        np = _np()
        x = pcm_to_float(pcm)
        if rate != MODEL_RATE:
            x = resample(x, rate)
        if len(x) < MIN_SECONDS * MODEL_RATE:
            return None
        x = x[: int(MAX_SECONDS * MODEL_RATE)]
        vec = self._model()(x)
        if vec is None:
            return None
        vec = np.asarray(vec, dtype=np.float32).reshape(-1)
        norm = float(np.linalg.norm(vec))
        return vec / norm if norm > 0 else None

    def warm(self) -> None:
        """Load the model and run it once so the first real utterance is not slow."""
        self._model()

    def _profile_matrices(self) -> dict:
        try:
            stamp = os.stat(profiles_path()).st_mtime_ns
        except OSError:
            stamp = None
        with self._profiles_lock:
            if stamp != self._profiles_stamp:
                np = _np()
                profiles = {}
                data = _load_profile_file() if stamp is not None else {"profiles": {}, "model": ""}
                if data.get("model") in ("", self.model):
                    for name, p in data["profiles"].items():
                        vecs = p.get("embeddings") or []
                        if vecs:
                            profiles[name] = np.asarray(vecs, dtype=np.float32)
                elif data["profiles"]:
                    LOG.warning("speaker profiles were made with model %r, not %r; ignored", data.get("model"), self.model)
                self._profiles, self._profiles_stamp = profiles, stamp
            return self._profiles

    def score(self, pcm: bytes, rate: int) -> dict:
        started = time.perf_counter()
        dur = len(pcm) // 2 / rate if rate else 0.0
        result = {"score": None, "best": None, "verdict": "unknown", "ms": 0.0, "dur": dur, "model": self.model}
        profiles = self._profile_matrices()
        if profiles:
            vec = self.embed(pcm, rate)
            if vec is not None:
                best, best_score = None, -2.0
                for name, matrix in profiles.items():
                    value = float((matrix @ vec).max())
                    if value > best_score:
                        best, best_score = name, value
                result.update(score=best_score, best=best, verdict="owner" if best_score >= self.threshold else "foreign")
        result["ms"] = (time.perf_counter() - started) * 1000.0
        return result

    def enrol(self, name: str, vectors) -> dict:
        """Append embeddings to profile `name` (existing ones are kept)."""
        if not isinstance(name, str) or not _NAME_RE.match(name.strip()):
            raise ValueError("invalid profile name")
        name = name.strip()
        vectors = [v for v in vectors if v is not None]
        if not vectors:
            raise ValueError("no usable audio (each sample needs at least 1 s)")
        with _PROFILE_LOCK:
            data = _load_profile_file()
            if data["profiles"] and data.get("model") not in ("", self.model):
                raise ValueError("existing profiles use model %r; delete them before enrolling with %r" % (data.get("model"), self.model))
            data["model"] = self.model
            profile = data["profiles"].setdefault(name, {"created": time.time(), "embeddings": []})
            profile["embeddings"].extend([round(float(x), 6) for x in v] for v in vectors)
            _save_profile_file(data)
            return {"name": name, "embeddings": len(profile["embeddings"]), "added": len(vectors), "created": profile["created"], "model": self.model}


_DEFAULT: Verifier | None = None
_DEFAULT_LOCK = threading.Lock()


def default_verifier() -> Verifier:
    global _DEFAULT
    with _DEFAULT_LOCK:
        if _DEFAULT is None:
            _DEFAULT = Verifier()
        return _DEFAULT


# ---------------------------------------------------------------- samples

def _write_wav(path: Path, samples_16k_pcm: bytes) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as handle, wave.open(handle, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(MODEL_RATE)
        out.writeframes(samples_16k_pcm)


def _to_16k_pcm(pcm: bytes, rate: int) -> bytes:
    if rate == MODEL_RATE:
        return pcm[: len(pcm) // 2 * 2]
    np = _np()
    y = resample(pcm_to_float(pcm), rate)
    return (np.clip(y, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()


def _sample_ids() -> list[str]:
    try:
        return sorted(p.stem for p in samples_dir().glob("*.wav") if _ID_RE.match(p.stem))
    except OSError:
        return []


def delete_sample(sample_id: str) -> bool:
    if not _ID_RE.match(sample_id or ""):
        return False
    found = False
    for suffix in (".wav", ".json"):
        try:
            (samples_dir() / (sample_id + suffix)).unlink()
            found = True
        except FileNotFoundError:
            pass
    return found


def capture_sample(pcm: bytes, rate: int, text: str, ts: float, info: dict | None = None) -> str | None:
    """Store a 16 kHz wav + metadata when the opt-in `capture` setting is on; prune by count and age."""
    settings = load_settings()
    if not settings["capture"] or not pcm:
        return None
    info = info or {}
    stamp = time.strftime("%Y%m%dT%H%M%S", time.localtime(ts))
    sample_id = "%s-%s" % (stamp, secrets.token_hex(3))
    pcm16 = _to_16k_pcm(pcm, rate)
    directory = samples_dir()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    _write_wav(directory / (sample_id + ".wav"), pcm16)
    meta = {"id": sample_id, "ts": ts, "text": text, "dur": len(pcm16) / 2 / MODEL_RATE, "rate": rate,
            "score": info.get("score"), "verdict": info.get("verdict")}
    _write_private(directory / (sample_id + ".json"), json.dumps(meta, ensure_ascii=False).encode())
    ids = _sample_ids()
    cutoff = time.time() - settings["capture_days"] * 86400
    keep = ids[-settings["capture_max"]:]
    for old in ids:
        if old not in keep or (old != sample_id and _sample_ts(old) < cutoff):
            delete_sample(old)
    return sample_id


def _sample_ts(sample_id: str) -> float:
    try:
        return float(json.loads((samples_dir() / (sample_id + ".json")).read_text())["ts"])
    except (OSError, ValueError, KeyError, TypeError):
        try:
            return (samples_dir() / (sample_id + ".wav")).stat().st_mtime
        except OSError:
            return 0.0


def list_samples() -> list[dict]:
    out = []
    for sample_id in reversed(_sample_ids()):
        try:
            meta = json.loads((samples_dir() / (sample_id + ".json")).read_text())
        except (OSError, ValueError):
            meta = {}
        meta["id"] = sample_id
        out.append(meta)
    return out


def sample_wav_path(sample_id: str) -> Path | None:
    if not _ID_RE.match(sample_id or ""):
        return None
    path = samples_dir() / (sample_id + ".wav")
    return path if path.is_file() else None


def _read_wav_pcm(path: Path) -> tuple[bytes, int]:
    with wave.open(str(path), "rb") as src:
        if src.getsampwidth() != 2:
            raise ValueError("%s: need 16-bit PCM wav" % path)
        channels, rate = src.getnchannels(), src.getframerate()
        pcm = src.readframes(src.getnframes())
    if channels > 1:
        np = _np()
        pcm = np.frombuffer(pcm, dtype="<i2").reshape(-1, channels)[:, 0].astype("<i2").tobytes()
    return pcm, rate


def enrol_from_samples(name: str, ids: list[str], verifier: Verifier | None = None) -> dict:
    verifier = verifier or default_verifier()
    vectors = []
    for sample_id in ids:
        path = sample_wav_path(sample_id)
        if path is None:
            raise ValueError("unknown sample " + str(sample_id))
        pcm, rate = _read_wav_pcm(path)
        vectors.append(verifier.embed(pcm, rate))
    return verifier.enrol(name, vectors)


# ---------------------------------------------------------------- score log

def record_score(db_path: str, row: dict) -> None:
    """Insert one verification result into the dialogs DB. Never raises."""
    try:
        conn = sqlite3.connect(db_path, timeout=30)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("CREATE TABLE IF NOT EXISTS voice_scores(id INTEGER PRIMARY KEY, ts REAL NOT NULL, dur REAL, text TEXT, "
                         "score REAL, best TEXT, verdict TEXT, mode TEXT, ms REAL, model TEXT)")
            conn.execute("CREATE INDEX IF NOT EXISTS voice_scores_ts ON voice_scores(ts)")
            conn.execute("INSERT INTO voice_scores(ts, dur, text, score, best, verdict, mode, ms, model) VALUES(?,?,?,?,?,?,?,?,?)",
                         (row.get("ts", time.time()), row.get("dur"), row.get("text"), row.get("score"), row.get("best"),
                          row.get("verdict"), row.get("mode"), row.get("ms"), row.get("model")))
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:  # scoring must never break the caller
        LOG.warning("voice_scores write failed class=%s", type(exc).__name__)


# ---------------------------------------------------------------- CLI

def _cli_enrol(args: list[str]) -> int:
    if len(args) < 2:
        print("usage: speakerid.py enrol NAME file.wav [...]", file=sys.stderr)
        return 2
    verifier = default_verifier()
    vectors = [verifier.embed(*_read_wav_pcm(Path(f))) for f in args[1:]]
    print(json.dumps(verifier.enrol(args[0], vectors), ensure_ascii=False))
    return 0


def _cli_score(args: list[str]) -> int:
    if len(args) != 1:
        print("usage: speakerid.py score file.wav", file=sys.stderr)
        return 2
    print(json.dumps(default_verifier().score(*_read_wav_pcm(Path(args[0]))), ensure_ascii=False))
    return 0


def _cli_bench(_args: list[str]) -> int:
    np = _np()
    verifier = default_verifier()
    rng = np.random.default_rng(0)
    pcm = (rng.standard_normal(48000 * 3) * 3000).astype("<i2").tobytes()
    verifier.embed(pcm, 48000)
    times = []
    for _ in range(20):
        started = time.perf_counter()
        verifier.embed(pcm, 48000)
        times.append((time.perf_counter() - started) * 1000)
    print(json.dumps({"model": verifier.model, "threads": verifier._threads, "median_ms_3s_48k": round(float(np.median(times)), 1)}))
    return 0


def main(argv: list[str]) -> int:
    commands = {"enrol": _cli_enrol, "score": _cli_score, "bench": _cli_bench}
    if not argv or argv[0] not in commands:
        print("usage: speakerid.py enrol NAME file.wav [...] | score file.wav | bench", file=sys.stderr)
        return 2
    return commands[argv[0]](argv[1:])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

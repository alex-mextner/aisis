"""Level front-end for the copy of an utterance that goes to a speech recognizer.

Far-field uplinks (the Yandex Station's vqe_0 is ~-75 dBFS RMS) leave Whisper-family recognizers deaf
or hallucinating. This lifts the speech to a target level, optionally tames loud parts with a gentle
compressor and always ends in a lookahead limiter, so loud speech is never clipped. Utterance-level
(offline, whole PCM in one call), stdlib + numpy only, so it is reusable by every caller that has one
utterance of mono s16le PCM (the STT gateway, stt-cli dictation).

The raw audio is never changed here: callers keep the original for archives and speaker checks.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import math

import numpy as np

FULL_SCALE = 32768.0
BLOCK_MS = 5            # gain-computer resolution of compressor and limiter
LOOKAHEAD_BLOCKS = 4    # limiter looks 20 ms ahead so a peak is already ducked when it arrives


@dataclass(frozen=True)
class FrontEndConfig:
    target_dbfs: float = -30.0        # speech RMS the output is lifted to; >= 0 disables the lift
    max_gain_db: float = 45.0         # never lift more than this: past it only the station's hiss gets louder
    highpass_hz: float = 0.0          # zero-phase rumble filter, 0 = off
    compressor: bool = False
    comp_threshold_dbfs: float = -20.0   # block level above which the compressor works
    comp_ratio: float = 2.5
    comp_attack_ms: float = 10.0
    comp_release_ms: float = 120.0
    limiter_dbfs: float = -2.0        # output peak ceiling

    @classmethod
    def from_env(cls, env, prefix="AISIS_STT_"):
        """Every knob as <prefix>NORM_DBFS, NORM_MAX_GAIN_DB, HIGHPASS_HZ, COMPRESSOR (0/1), COMP_THRESHOLD_DBFS,
        COMP_RATIO, LIMITER_DBFS; garbage falls back to the default."""
        def number(name, default, cast=float):
            try:
                return cast(env.get(prefix + name, default))
            except (TypeError, ValueError):
                return default
        base = cls()
        return cls(target_dbfs=number("NORM_DBFS", base.target_dbfs),
                   max_gain_db=max(0.0, number("NORM_MAX_GAIN_DB", base.max_gain_db)),
                   highpass_hz=max(0.0, number("HIGHPASS_HZ", base.highpass_hz)),
                   compressor=bool(number("COMPRESSOR", int(base.compressor), int)),
                   comp_threshold_dbfs=number("COMP_THRESHOLD_DBFS", base.comp_threshold_dbfs),
                   comp_ratio=max(1.0, number("COMP_RATIO", base.comp_ratio)),
                   limiter_dbfs=min(0.0, number("LIMITER_DBFS", base.limiter_dbfs)))

    def with_(self, **changes):
        return replace(self, **changes)


def _db(value):
    return 20.0 * math.log10(max(value, 1e-9))


def estimate_speech_rms(samples, rate):
    """RMS (LSB) of the loudest 40 % of 20 ms frames: a VAD-free stand-in for 'voiced RMS'."""
    frame = max(1, rate // 50)
    count = len(samples) // frame
    if count == 0:
        return float(np.sqrt(np.mean(samples * samples))) if len(samples) else 0.0
    energy = np.mean(samples[:count * frame].reshape(count, frame) ** 2, axis=1)
    top = np.sort(energy)[::-1][:max(1, int(count * 0.4))]
    return float(math.sqrt(float(np.mean(top))))


def _highpass(samples, rate, cutoff):
    """Zero-phase FFT high-pass with a 1/3-octave raised-cosine skirt below `cutoff`."""
    spectrum = np.fft.rfft(samples)
    freqs = np.fft.rfftfreq(len(samples), 1.0 / rate)
    low = cutoff * 0.5
    ramp = np.clip((freqs - low) / max(cutoff - low, 1e-6), 0.0, 1.0)
    spectrum *= 0.5 - 0.5 * np.cos(math.pi * ramp)
    return np.fft.irfft(spectrum, len(samples))


def _block_peaks(samples, block):
    count = -(-len(samples) // block)
    padded = np.zeros(count * block, dtype=np.float32)
    padded[:len(samples)] = np.abs(samples)
    return padded.reshape(count, block).max(axis=1)


def _smooth(targets_db, attack, release):
    """One-pole smoothing of a gain curve in dB: fast when the gain must fall, slow when it recovers."""
    out = np.empty_like(targets_db)
    state = 0.0
    for index, target in enumerate(targets_db.tolist()):
        coef = attack if target < state else release
        state = target + (state - target) * coef
        out[index] = state
    return out


def _apply_block_gain(samples, gains_db, block):
    """Linear-in-dB interpolation of per-block gains to per-sample gains (no zipper noise)."""
    centers = (np.arange(len(gains_db)) + 0.5) * block
    per_sample = np.interp(np.arange(len(samples)), centers, gains_db)
    return samples * np.power(10.0, per_sample / 20.0).astype(np.float32)


def _compress(samples, rate, cfg):
    block = max(1, rate * BLOCK_MS // 1000)
    peaks = _block_peaks(samples, block) / FULL_SCALE
    level_db = 20.0 * np.log10(np.maximum(peaks, 1e-9))
    over = np.maximum(level_db - cfg.comp_threshold_dbfs, 0.0)
    reduction = -over * (1.0 - 1.0 / cfg.comp_ratio)
    attack = math.exp(-BLOCK_MS / max(cfg.comp_attack_ms, 0.1))
    release = math.exp(-BLOCK_MS / max(cfg.comp_release_ms, 0.1))
    return _apply_block_gain(samples, _smooth(reduction, attack, release), block)


def _limit(samples, rate, ceiling_dbfs):
    """Lookahead limiter: block gain = ceiling / peak (min over the lookahead window), released smoothly."""
    block = max(1, rate * BLOCK_MS // 1000)
    ceiling = 10 ** (ceiling_dbfs / 20) * FULL_SCALE
    peaks = np.maximum(_block_peaks(samples, block), 1e-9)
    needed = np.minimum(0.0, 20.0 * np.log10(ceiling / peaks))
    window = np.lib.stride_tricks.sliding_window_view(np.pad(needed, (LOOKAHEAD_BLOCKS, LOOKAHEAD_BLOCKS), constant_values=0.0),
                                                      2 * LOOKAHEAD_BLOCKS + 1).min(axis=1)
    release = math.exp(-BLOCK_MS / 60.0)
    smoothed = _smooth(window, 0.0, release)
    limited = _apply_block_gain(samples, np.minimum(smoothed, window), block)
    return np.clip(limited, -ceiling, ceiling)


def process(pcm, rate, cfg=None, voiced_rms=None):
    """(pcm, info) for mono s16le `pcm`. info: gain_db (lift), compressed (bool), in_rms/out_rms (LSB), out_peak.
    The input is returned untouched (same bytes object) when nothing had to change."""
    cfg = cfg or FrontEndConfig()
    samples = np.frombuffer(pcm[:len(pcm) - len(pcm) % 2], dtype="<i2").astype(np.float32)
    info = {"gain_db": 0.0, "compressed": False, "in_rms": 0.0, "out_rms": 0.0, "out_peak": 0}
    if not len(samples):
        return pcm, info
    speech = voiced_rms if voiced_rms and voiced_rms > 0 else estimate_speech_rms(samples, rate)
    info["in_rms"] = round(speech, 1)
    gain_db = 0.0
    if cfg.target_dbfs < 0 and speech > 0:
        gain_db = min(cfg.target_dbfs - _db(speech / FULL_SCALE), cfg.max_gain_db)
        peak = float(np.max(np.abs(samples)))
        if peak > 0:
            gain_db = min(gain_db, cfg.limiter_dbfs - _db(peak / FULL_SCALE) + (12.0 if cfg.compressor else 0.0))
    gain_db = max(gain_db, 0.0)
    if gain_db <= 0.4 and not cfg.highpass_hz and not cfg.compressor:
        return pcm, info
    out = samples
    if cfg.highpass_hz:
        out = _highpass(out, rate, cfg.highpass_hz).astype(np.float32)
    if gain_db > 0.4:
        out = out * np.float32(10 ** (gain_db / 20))
    if cfg.compressor:
        out = _compress(out, rate, cfg)
        info["compressed"] = True
    out = _limit(out, rate, cfg.limiter_dbfs)
    out_i16 = np.clip(np.rint(out), -32768, 32767).astype("<i2")
    info.update(gain_db=round(gain_db, 1), out_rms=round(estimate_speech_rms(out_i16.astype(np.float32), rate), 1),
                out_peak=int(np.max(np.abs(out_i16))))
    return out_i16.tobytes(), info

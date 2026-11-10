"""Overlay read-modify-write must not lose concurrent dashboard updates (#75)."""
from __future__ import annotations

import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import speaker_settings


def test_concurrent_overlay_updates_are_all_kept(tmp_path, monkeypatch):
    path = tmp_path / "overlay.json"
    real_load = speaker_settings.load_overlay

    def slow_load(p=None):
        data = real_load(p)
        threading.Event().wait(0.02)    # widen the read-to-write window so unserialized writers interleave
        return data

    monkeypatch.setattr(speaker_settings, "load_overlay", slow_load)
    keys = ["account.jingle", "account.aliceProactivity", "device.dnd", "device.led.idleAnimation",
            "account.crossfade", "device.standby.enabled"]
    barrier = threading.Barrier(len(keys))

    def write(key):
        barrier.wait()
        speaker_settings.save_overlay_changes({key: True}, path)

    threads = [threading.Thread(target=write, args=(k,)) for k in keys]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    expected = {}
    for k in keys:
        expected = speaker_settings.deep_merge(expected, speaker_settings.sync_overlay({k: True}))
    assert real_load(path) == expected

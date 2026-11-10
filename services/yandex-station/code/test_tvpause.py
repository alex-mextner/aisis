"""TV pause: hold-timer rearm while HA is down, and idle-check + resume atomicity (#75)."""
from __future__ import annotations

import errno
import fcntl
import os
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from tvpause import TvPause

ENTITY = "media_player.televizor"


class FakeHa:
    def __init__(self):
        self.up = True
        self.tv = "playing"
        self.calls: list[str] = []

    def ha_up(self):
        return self.up

    def state(self, entity):
        return self.tv, False

    def call(self, service, entity):
        self.calls.append(service)
        self.tv = "paused" if service == "media_pause" else "playing"
        return True


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def rig(tmp_path):
    ha, clock = FakeHa(), Clock()
    settings = {"enabled": True, "resume": True, "entities": [ENTITY], "resume_delay_s": 1.5, "max_hold_s": 300}
    tv = TvPause(ha=ha, settings=lambda: settings, state_file=tmp_path / "state.json", dry_run=False, clock=clock)
    yield tv, ha, clock
    for timer in tv.timers.values():
        timer.cancel()


def test_second_activation_while_ha_down_rearms_hold_timer(rig):
    tv, ha, clock = rig
    tv.activation()
    assert ha.calls == ["media_pause"]
    first = clock.now
    clock.now += 50
    ha.up = False
    tv.activation()
    assert tv.timers["hold"].args == (clock.now,) != (first,)
    # the rearmed timer must actually resume the pause we still hold
    ha.up = True
    tv._hold_expired(clock.now)
    assert ha.calls == ["media_pause", "media_play"]
    assert tv.status()["paused"] == {}


def test_stale_hold_timer_is_a_noop_after_newer_activation(rig):
    tv, ha, clock = rig
    tv.activation()
    old = clock.now
    clock.now += 10
    tv.activation()
    tv._hold_expired(old)
    assert ha.calls == ["media_pause"]
    assert ENTITY in tv.status()["paused"]


def test_activation_during_grace_cancels_resume_atomically(rig):
    tv, ha, clock = rig
    tv.activation()
    ended = clock.now + 1
    clock.now += 2
    tv.activation()                      # next turn starts inside the grace window
    tv._resume_if_idle(ended)
    assert ha.calls == ["media_pause"]
    assert ENTITY in tv.status()["paused"]


def test_resume_guard_is_evaluated_under_the_state_lock(rig):
    tv, ha, clock = rig
    tv.activation()
    seen = []

    def guard(st):
        seen.append(ENTITY in st["paused"])
        return False

    tv.resume("dialog ended", unless=guard)
    assert seen == [True]
    assert ha.calls == ["media_pause", "media_play"]


def assert_state_lock_held(tv):
    fd = os.open(str(tv.state_file) + ".lock", os.O_RDWR)
    try:
        with pytest.raises(OSError) as info:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert info.value.errno in (errno.EWOULDBLOCK, errno.EAGAIN)
    finally:
        os.close(fd)


@pytest.mark.parametrize("callback", ["hold", "idle"])
def test_resume_callbacks_check_and_release_under_one_lock(rig, callback):
    tv, ha, clock = rig
    tv.activation()
    checked = []
    original = tv.resume

    def spy(reason, unless=None):
        def guard(st):
            assert_state_lock_held(tv)
            checked.append(True)
            return unless(st)
        original(reason, unless=guard)

    tv.resume = spy
    ha_state = ha.state

    def locked_state(entity):
        assert_state_lock_held(tv)           # the release happens under the same lock as the guard
        return ha_state(entity)

    ha.state = locked_state
    if callback == "hold":
        tv._hold_expired(clock.now)
    else:
        tv._resume_if_idle(clock.now)
    assert checked == [True]
    assert ha.calls == ["media_pause", "media_play"]


def test_older_activation_cannot_replace_newer_hold_timer(rig):
    tv, ha, clock = rig
    tv.activation()                          # pause held, so an HA-down activation still arms a hold
    ticks = iter(range(2000, 2100))
    tv.clock = lambda: float(next(ticks))
    newer_done = threading.Event()
    older_arming = threading.Event()
    original_arm = tv._arm
    armed = []
    errors = []

    def arm(name, delay, fn, *args):
        if not armed:                        # the older activation, about to arm its timer
            armed.append(args)
            older_arming.set()
            newer_done.wait(0.5)             # with the lock held it times out; unlocked, the newer one finishes here
        original_arm(name, delay, fn, *args)

    def run(fn):
        try:
            fn()
        except BaseException as error:       # surfaced by the assertions below
            errors.append(error)

    tv._arm = arm
    ha.up = False
    older = threading.Thread(target=run, args=(tv.activation,))
    older.start()
    assert older_arming.wait(5), "older activation never reached the hold timer"
    ha.up = True

    def newer():
        tv.activation()
        newer_done.set()

    newer_thread = threading.Thread(target=run, args=(newer,))
    newer_thread.start()
    older.join(5)
    newer_thread.join(5)
    assert not older.is_alive() and not newer_thread.is_alive()
    assert not errors, errors
    assert tv.timers["hold"].args == (tv.status()["last_activation"],)

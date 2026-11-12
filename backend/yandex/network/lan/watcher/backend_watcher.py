#!/usr/bin/env python3
"""Fail-open watcher for the DNS relay (runs next to it on the always-on LAN host, no dependency on the backend host).

The relay overrides the Yandex names for the speaker only while `enabled` is true in its config.json (re-read on
every DNS request). That override points the speaker at the TLS front, which proxies to the Alice backend on another host.
This watcher TCP-connects to that backend every few seconds and flips ONLY the `enabled` field:

* UP   -> DOWN after N consecutive failed checks: the relay forwards everything to the ISP upstreams (cloud mode),
* DOWN -> UP   after M consecutive successful checks: the override is switched back on.

An `enabled=false` written by anyone else (operator `router_control.py forwarding --apply`, a hand edit, a seeded
config) is a MANUAL HOLD: the watcher adopts it and never switches the override back on by itself — not even in
mode=forwarding, where the old per-list domains would silently turn into the primary target. Only a DOWN the watcher
itself wrote (marked by config.json.watcher-down, so the intent survives a restart) is automatically undone.

A check succeeds only if EVERY target accepts a TCP connection (HTTP status is deliberately not used: a plain GET / on the
backend may legitimately return an error status). The write is atomic (temp file + rename in the same directory, mode 600) and every other
byte of config.json is kept; a copy of the healthy config is kept as config.json.healthy. At start an unreachable
backend means DOWN immediately (fail-open default). Each transition is logged to stdout with a timestamp.
"""
import argparse
import hashlib
import contextlib
import json
import os
import re
import socket
import sys
import tempfile
import time
from datetime import datetime

ENABLED = re.compile(rb'("enabled"\s*:\s*)(true|false)')


def log(message):
    print(f"{datetime.now().astimezone().isoformat(timespec='seconds')} backend-watcher: {message}", flush=True)


def check_target(target, timeout):
    """None when the TCP connect succeeds, else a short reason."""
    host, port = target
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return None
    except socket.timeout:
        return "timeout"
    except OSError as exc:
        return exc.__class__.__name__


def parse_config(raw):
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise ValueError("config.json is not a JSON object")
    return parsed


def config_enabled(raw):
    value = parse_config(raw).get("enabled")
    if not isinstance(value, bool):
        raise ValueError('"enabled" is not a boolean')
    return value


def with_enabled(raw, value):
    """raw config bytes with only the enabled literal replaced; refuses anything ambiguous."""
    if len(ENABLED.findall(raw)) != 1:
        raise ValueError('config.json must contain exactly one "enabled": true|false literal')
    new = ENABLED.sub(lambda m: m.group(1) + (b"true" if value else b"false"), raw, count=1)
    expected = json.loads(raw)
    expected["enabled"] = value
    if json.loads(new) != expected:
        raise ValueError("replacement would change more than the top-level enabled field")
    return new


def atomic_write(path, data):
    directory = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(prefix="." + os.path.basename(path) + ".", dir=directory)
    try:
        with os.fdopen(fd, "wb") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise
    dir_fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


class Watcher:
    def __init__(self, config, targets, timeout=1.5, down_after=3, up_after=3, template=None):
        self.config = config
        self.template = template or config + ".healthy"
        self.targets = targets
        self.timeout = timeout
        self.down_after = down_after
        self.up_after = up_after
        self.up = False  # mirrors config enabled
        self.hold = False  # True while an outside enabled=false must not be auto-undone
        self.fails = 0
        self.oks = 0

    def down_marker(self):
        return self.config + ".watcher-down"

    def mark_own_down(self, raw):
        # the marker records the exact bytes of our DOWN config: any later difference is an outside edit
        atomic_write(self.down_marker(), hashlib.sha256(raw).hexdigest().encode())

    def own_down(self):
        """True while config.json still holds exactly the DOWN bytes the watcher wrote."""
        try:
            with open(self.down_marker(), "rb") as handle:
                marked = handle.read().decode()
            return hashlib.sha256(self.read()).hexdigest() == marked
        except (OSError, UnicodeDecodeError):
            return False

    def clear_own_down(self):
        with contextlib.suppress(FileNotFoundError):
            os.unlink(self.down_marker())

    def read(self):
        with open(self.config, "rb") as handle:
            return handle.read()

    def failing(self):
        return [f"{host}:{port} {reason}" for (host, port) in self.targets
                if (reason := check_target((host, port), self.timeout))]

    def refresh_template(self, raw):
        try:
            with open(self.template, "rb") as handle:
                if handle.read() == raw:
                    return
        except FileNotFoundError:
            pass
        atomic_write(self.template, raw)
        log(f"healthy template {os.path.basename(self.template)} updated")

    def set_enabled(self, value):
        raw = self.read()
        if config_enabled(raw) != value:
            raw = with_enabled(raw, value)
            atomic_write(self.config, raw)
        if value:
            self.refresh_template(raw)
        self.up = value

    def start(self):
        try:
            self.up = config_enabled(self.read())
        except PermissionError as exc:
            # unreadable is not corrupted: never replace a config we cannot even read
            log(f"config.json is not readable ({exc}); not touching it")
            raise
        except (OSError, ValueError) as exc:
            log(f"config.json unusable ({exc.__class__.__name__}: {exc}); restoring DOWN from the healthy template")
            with open(self.template, "rb") as handle:
                raw = with_enabled(handle.read(), False)
            try:
                self.mark_own_down(raw)  # the marker first: an unmarked DOWN would read as a manual hold after a crash
            except OSError as exc:
                log(f"cannot write the watcher marker ({exc}); not touching config.json")
                raise
            atomic_write(self.config, raw)
            self.up = False
        if self.up:
            self.clear_own_down()
        if not self.up and not self.own_down():
            self.hold = True  # enabled=false that nobody of ours wrote (or was edited since): a manual hold
        bad = self.failing()
        log(f"start: config enabled={str(self.up).lower()}{', manual hold' if self.hold else ''}, "
            f"targets {self.describe(bad)}")
        if bad:
            if self.up:
                self.flip(False, f"backend unreachable at start: {', '.join(bad)}")
            self.fails = self.down_after
        elif self.up:
            self.refresh_template(self.read())

    def describe(self, bad):
        return "all reachable" if not bad else "unreachable: " + ", ".join(bad)

    def step(self):
        try:
            raw = self.read()
            current = config_enabled(raw)
        except (OSError, ValueError) as exc:
            log(f"cannot read config.json ({exc.__class__.__name__}: {exc}); relay already fails closed to forwarding")
            current, raw = self.up, None
        if current != self.up:
            log(f"config.json changed outside the watcher: enabled={str(current).lower()} (adopting, streaks reset)")
            self.up, self.fails, self.oks = current, 0, 0
            self.clear_own_down()
            if not current:
                self.hold = True  # an outside DOWN is the operator's decision, not ours to undo
                log("manual hold: the watcher will not switch the override back on by itself")
            else:
                self.hold = False
        elif not current and raw is not None and not self.hold and not self.own_down():
            # the file was rewritten outside the watcher while DOWN (e.g. forwarding --apply keeps enabled=false):
            # treating the new bytes as ours would silently re-enable the override later
            self.hold = True
            self.clear_own_down()
            log("manual hold: config.json was changed outside the watcher while enabled=false")
        bad = self.failing()
        if bad:
            self.fails, self.oks = self.fails + 1, 0
            if self.up:
                log(f"check failed {self.fails}/{self.down_after}: {', '.join(bad)}")
                if self.fails >= self.down_after:
                    self.flip(False, f"{self.fails} consecutive failures: {', '.join(bad)}")
        else:
            self.oks, self.fails = self.oks + 1, 0
            if not self.up:
                if self.hold:
                    return  # under a manual hold even M successes never re-enable
                log(f"check ok {self.oks}/{self.up_after}")
                if self.oks >= self.up_after:
                    self.flip(True, f"{self.oks} consecutive successes")

    def flip(self, value, reason):
        before = self.up
        try:
            raw = self.read()
            new = raw
            if config_enabled(raw) != value:
                new = with_enabled(raw, value)
                if not value:
                    try:
                        self.mark_own_down(new)  # marker first: a crash between the writes must not read as a hold
                    except OSError as exc:
                        log(f"FAILED to write the watcher marker ({exc}); leaving enabled={str(self.up).lower()}")
                        return
                atomic_write(self.config, new)
            if value:
                self.refresh_template(new)  # the flipped bytes, not the pre-write ones: the template is the healthy copy
        except (OSError, ValueError) as exc:
            log(f"FAILED to write enabled={str(value).lower()} ({exc.__class__.__name__}: {exc}); will retry")
            return
        if value:
            try:
                self.clear_own_down()
            except OSError as exc:
                log(f"cannot remove the watcher marker ({exc}); continuing")
        self.up = value
        self.fails = self.oks = 0
        log(f"TRANSITION {'UP' if before else 'DOWN'}->{'UP' if value else 'DOWN'}: enabled={str(value).lower()} ({reason})")



def parse_target(text):
    host, _, port = text.rpartition(":")
    if not host or not port.isdigit():
        raise argparse.ArgumentTypeError(f"target must be HOST:PORT, got {text!r}")
    return host, int(port)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", default="/config/config.json")
    parser.add_argument("--target", action="append", type=parse_target, metavar="HOST:PORT",
                        help="backend endpoint to probe (repeatable); default: AISIS_WATCHER_TARGETS, space separated")
    parser.add_argument("--interval", type=float, default=5.0)
    parser.add_argument("--timeout", type=float, default=1.5)
    parser.add_argument("--down-after", type=int, default=3)
    parser.add_argument("--up-after", type=int, default=3)
    args = parser.parse_args()
    if not args.target:
        try:
            args.target = [parse_target(item) for item in os.environ.get("AISIS_WATCHER_TARGETS", "").split()]
        except argparse.ArgumentTypeError as exc:
            parser.error(f"AISIS_WATCHER_TARGETS: {exc}")
    if not args.target:
        parser.error("give at least one --target HOST:PORT (or set AISIS_WATCHER_TARGETS)")
    watcher = Watcher(args.config, args.target, args.timeout, args.down_after, args.up_after)
    try:
        watcher.start()
    except (OSError, ValueError) as exc:
        log(f"cannot start: {exc.__class__.__name__}: {exc}")
        return 2
    while True:
        started = time.monotonic()
        watcher.step()
        time.sleep(max(0.0, args.interval - (time.monotonic() - started)))


if __name__ == "__main__":
    sys.exit(main())

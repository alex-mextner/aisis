"""backend_watcher: hysteresis, atomic write, untouched fields, restart sanity, fail-open start, and the real relay.

Runs entirely in a temp dir with a fake TCP backend; never reads or writes the live config.
  python3 -m unittest -v test_backend_watcher
The relay tests run the repo's dns_forwarder.py (env DNS_FORWARDER overrides the path) and need TEST_UPSTREAM_IP, a
non-loopback IPv4 address this machine owns, for the fake upstream DNS server; they skip without it.
"""
import contextlib
import io
import json
import os
from pathlib import Path
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

import backend_watcher as bw

HERE = Path(__file__).resolve().parent
DNS_FORWARDER = os.environ.get("DNS_FORWARDER", str(HERE.parent.parent / "dns_forwarder.py"))
UPSTREAM_IP = os.environ.get("TEST_UPSTREAM_IP", "")

CONFIG_TEXT = """{
  "bind_ip": "127.0.0.1",
  "port": 15353,
  "upstreams": [
    "192.0.2.53"
  ],
  "timeout": 1.5,
  "enabled": true,
  "target": {
    "ip": "127.0.0.1",
    "mac": "aa:bb:cc:dd:ee:ff"
  },
  "route_ip": "127.0.0.1",
  "mode": "voice",
  "domains": [
    "quasar.yandex.net",
    "uniproxy.alice.yandex.net"
  ],
  "ttl": 5,
  "router": {"host": "192.0.2.1", "server_mac": "00:11:22:33:44:55"},
  "voice_targets": [{"ip": "127.0.0.1", "mac": "aa:bb:cc:dd:ee:ff"}]
}
"""


class FakeBackend:
    """A TCP listener we can stop and start again on the same port; it accepts and drops connections."""

    def __init__(self):
        self.port = None
        self.sock = None
        self.start()

    def start(self):
        sock = socket.socket()
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", self.port or 0))
        sock.listen(16)
        self.port = sock.getsockname()[1]
        self.sock = sock
        threading.Thread(target=self.accept_loop, args=(sock,), daemon=True).start()

    @staticmethod
    def accept_loop(sock):
        while True:
            try:
                conn, _ = sock.accept()
            except OSError:
                return
            conn.close()

    def stop(self):
        if self.sock:
            try:
                self.sock.shutdown(socket.SHUT_RDWR)  # wakes the blocked accept(): close() alone keeps the port listening
            except OSError:
                pass
            self.sock.close()
            self.sock = None


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.config = self.dir / "config.json"
        self.config.write_text(CONFIG_TEXT)
        self.config.chmod(0o600)
        self.a, self.b = FakeBackend(), FakeBackend()
        self.addCleanup(self.a.stop)
        self.addCleanup(self.b.stop)

    def watcher(self, **kwargs):
        kwargs.setdefault("timeout", 0.3)
        return bw.Watcher(str(self.config), [("127.0.0.1", self.a.port), ("127.0.0.1", self.b.port)], **kwargs)

    def started(self, **kwargs):
        w = self.watcher(**kwargs)
        with contextlib.redirect_stdout(io.StringIO()):
            w.start()
        return w

    def step(self, w, times=1):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            for _ in range(times):
                w.step()
        return buf.getvalue()

    def enabled(self):
        return json.loads(self.config.read_text())["enabled"]


class Hysteresis(Base):
    def test_healthy_start_changes_nothing(self):
        before = self.config.stat()
        w = self.started()
        after = self.config.stat()
        self.assertTrue(w.up)
        self.assertEqual((before.st_ino, before.st_mtime_ns), (after.st_ino, after.st_mtime_ns))
        self.assertEqual(self.config.read_text(), CONFIG_TEXT)

    def test_healthy_to_down_to_up_with_hysteresis(self):
        w = self.started()
        self.a.stop()
        self.step(w, 2)
        self.assertTrue(self.enabled(), "2 failures must not flip yet")
        out = self.step(w)
        self.assertFalse(self.enabled(), "3rd consecutive failure flips to DOWN")
        self.assertIn("TRANSITION UP->DOWN", out)
        self.assertFalse(w.up)
        self.a.start()
        self.step(w, 2)
        self.assertFalse(self.enabled(), "2 successes must not flip back yet")
        out = self.step(w)
        self.assertTrue(self.enabled(), "3rd consecutive success flips back to UP")
        self.assertIn("TRANSITION DOWN->UP", out)
        self.assertEqual(self.config.read_text(), CONFIG_TEXT, "UP restores the original bytes exactly")

    def test_failures_must_be_consecutive(self):
        w = self.started()
        for _ in range(4):
            self.a.stop()
            self.step(w, 2)
            self.a.start()
            self.step(w)
        self.assertTrue(self.enabled())

    def test_up_streak_must_be_consecutive(self):
        w = self.started()
        self.b.stop()
        self.step(w, 3)
        self.assertFalse(self.enabled())
        for _ in range(3):
            self.b.start()
            self.step(w, 2)
            self.b.stop()
            self.step(w)
        self.assertFalse(self.enabled())

    def test_every_target_must_be_reachable(self):
        w = self.started()
        self.b.stop()  # only the second target is down
        self.step(w, 3)
        self.assertFalse(self.enabled())

    def test_thresholds_are_configurable(self):
        w = self.started(down_after=1, up_after=2)
        self.a.stop()
        self.step(w)
        self.assertFalse(self.enabled())
        self.a.start()
        self.step(w)
        self.assertFalse(self.enabled())
        self.step(w)
        self.assertTrue(self.enabled())


class WriteSafety(Base):
    def flip_down(self):
        w = self.started()
        self.a.stop()
        self.step(w, 3)
        return w

    def test_only_enabled_changes_byte_for_byte(self):
        self.flip_down()
        expected = CONFIG_TEXT.replace('"enabled": true', '"enabled": false')
        self.assertEqual(self.config.read_text(), expected)
        self.assertEqual(len(expected), len(CONFIG_TEXT) + 1)

    def test_write_is_atomic_rename_mode_600_no_leftovers(self):
        before = self.config.stat().st_ino
        self.flip_down()
        st = self.config.stat()
        self.assertNotEqual(st.st_ino, before, "new inode = temp file renamed over the old one")
        self.assertEqual(stat.S_IMODE(st.st_mode), 0o600)
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["config.json", "config.json.healthy", "config.json.watcher-down"])

    def test_reader_never_sees_a_partial_file(self):
        w = self.started()
        stop, bad = threading.Event(), []

        def reader():
            while not stop.is_set():
                try:
                    json.loads(self.config.read_text())
                except ValueError as exc:
                    bad.append(exc)

        thread = threading.Thread(target=reader)
        thread.start()
        try:
            for _ in range(60):
                w.set_enabled(False)
                w.set_enabled(True)
        finally:
            stop.set()
            thread.join()
        self.assertEqual(bad, [])

    def test_failed_replace_leaves_original_and_no_temp_file(self):
        w = self.started()
        with mock.patch.object(os, "replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                w.set_enabled(False)
        self.assertEqual(self.config.read_text(), CONFIG_TEXT)
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["config.json", "config.json.healthy"])

    def test_write_error_in_step_is_logged_and_retried(self):
        w = self.started()
        self.a.stop()
        with mock.patch.object(bw, "atomic_write", side_effect=OSError("read-only file system")):
            out = self.step(w, 3)
        self.assertIn("FAILED to write the watcher marker", out)  # the marker is written first: it fails before config.json
        self.assertTrue(self.enabled())
        out = self.step(w)  # backend still down: the retry succeeds without the mock
        self.assertFalse(self.enabled())

    def test_ambiguous_config_is_refused(self):
        twice = CONFIG_TEXT.replace('"ttl": 5,', '"ttl": 5,\n  "extra": {"enabled": true},')
        with self.assertRaises(ValueError):
            bw.with_enabled(twice.encode(), False)
        with self.assertRaises(ValueError):
            bw.with_enabled(b'{"enabled": "yes"}', False)

    def test_template_is_kept_and_is_the_healthy_config(self):
        w = self.started()
        template = self.dir / "config.json.healthy"
        self.assertEqual(template.read_text(), CONFIG_TEXT)
        self.assertEqual(stat.S_IMODE(template.stat().st_mode), 0o600)
        self.a.stop()
        self.step(w, 3)
        self.assertEqual(template.read_text(), CONFIG_TEXT, "template keeps enabled=true while the config is DOWN")


class OperatorHold(Base):
    """The watcher must never re-enable over an operator's edit, even one that keeps enabled=false."""

    def test_outside_rewrite_while_down_is_a_hold(self):
        w = self.started()
        self.a.stop()
        self.step(w, 3)  # watcher DOWN: enabled=false + hash marker
        self.assertFalse(self.enabled())
        # operator: `forwarding --apply` rewrites the config but keeps enabled=false
        cfg = json.loads(self.config.read_text())
        cfg["mode"] = "forwarding"
        self.config.write_text(json.dumps(cfg, indent=2))
        self.a.start()
        out = self.step(w, 5)
        self.assertIn("manual hold", out)
        self.assertFalse(self.enabled(), "the override must stay off over the operator's forwarding config")

    def test_no_stale_marker_hold_after_outside_reenable(self):
        w = self.started()
        self.a.stop()
        self.step(w, 3)
        self.config.write_text(CONFIG_TEXT)  # operator re-enables by hand
        self.a.start()
        self.step(w, 3)
        self.assertTrue(self.enabled())
        cfg = json.loads(self.config.read_text())
        cfg["enabled"] = False
        self.config.write_text(json.dumps(cfg))  # operator disables again
        w2 = self.started()
        self.assertTrue(w2.hold, "an operator DOWN must be a hold, not auto-undone via a stale marker")

class RestartSanity(Base):
    def test_unreachable_at_start_is_down_immediately(self):
        self.a.stop()
        w = self.watcher()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            w.start()
        self.assertFalse(self.enabled(), "fail-open default: no waiting for 3 failures")
        self.assertIn("TRANSITION UP->DOWN", buf.getvalue())
        self.assertIn("unreachable at start", buf.getvalue())

    def test_restart_while_down_and_backend_back_still_needs_three_successes(self):
        w = self.started()
        self.a.stop()
        self.step(w, 3)
        self.a.start()
        w2 = self.started()  # crash/restart of the watcher
        self.assertFalse(w2.up)
        self.step(w2, 2)
        self.assertFalse(self.enabled())
        self.step(w2)
        self.assertTrue(self.enabled())

    def test_restart_mid_failure_streak_with_backend_still_down(self):
        w = self.started()
        self.a.stop()
        self.step(w, 2)
        self.assertTrue(self.enabled())
        w2 = self.started()  # restarted before the 3rd failure: start() sees it is down now
        self.assertFalse(self.enabled())
        self.assertFalse(w2.up)

    def test_restart_up_and_healthy_keeps_state_and_file(self):
        self.started()
        before = self.config.stat().st_ino
        w2 = self.started()
        self.assertTrue(w2.up)
        self.assertEqual(self.config.stat().st_ino, before)

    def test_manual_edit_is_adopted(self):
        w = self.started()
        self.config.write_text(CONFIG_TEXT.replace('"enabled": true', '"enabled": false'))
        out = self.step(w)
        self.assertIn("changed outside the watcher", out)
        self.assertFalse(w.up)

    def test_operator_disable_is_a_hold_not_undone_by_health(self):
        w = self.started()
        self.config.write_text(CONFIG_TEXT.replace('"enabled": true', '"enabled": false'))
        out = self.step(w, 5)  # backend healthy the whole time
        self.assertFalse(self.enabled(), "the watcher must not switch an operator's enabled=false back on")
        self.assertIn("manual hold", out)
        self.assertTrue(w.hold)

    def test_hold_survives_a_watcher_restart(self):
        self.config.write_text(CONFIG_TEXT.replace('"enabled": true', '"enabled": false'))
        w = self.started()  # no marker file: this DOWN is not ours
        self.step(w, 5)
        self.assertFalse(self.enabled())

    def test_hold_is_released_when_the_operator_re_enables(self):
        w = self.started()
        self.config.write_text(CONFIG_TEXT.replace('"enabled": true', '"enabled": false'))
        self.step(w)
        self.config.write_text(CONFIG_TEXT)
        self.step(w)
        self.assertTrue(w.up)
        self.assertFalse(w.hold)
        self.a.stop()
        self.step(w, 3)
        self.assertFalse(self.enabled(), "after the hold is released, normal hysteresis applies again")

    def test_corrupt_config_at_start_is_restored_down_from_template(self):
        self.started()
        self.config.write_text("{not json")
        w = self.started()
        self.assertFalse(w.up)
        self.assertEqual(self.config.read_text(), CONFIG_TEXT.replace('"enabled": true', '"enabled": false'))

    def test_missing_config_and_template_refuses_to_start(self):
        self.config.unlink()
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(subprocess_main(["--config", str(self.config), "--target", "127.0.0.1:1"]), 2)


class Targets(Base):
    def test_targets_come_from_the_environment_when_no_flag_is_given(self):
        env = {"AISIS_WATCHER_TARGETS": f"127.0.0.1:{self.a.port} 127.0.0.1:{self.b.port}"}
        with mock.patch.dict(os.environ, env), mock.patch.object(bw.Watcher, "start", side_effect=SystemExit(0)) as start, \
                mock.patch.object(bw, "Watcher", wraps=bw.Watcher) as watcher_cls:
            with self.assertRaises(SystemExit):
                subprocess_main(["--config", str(self.config)])
        self.assertEqual(watcher_cls.call_args.args[1], [("127.0.0.1", self.a.port), ("127.0.0.1", self.b.port)])
        start.assert_called_once()

    def test_no_target_anywhere_is_a_usage_error(self):
        with mock.patch.dict(os.environ, {"AISIS_WATCHER_TARGETS": ""}), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as caught:
                subprocess_main(["--config", str(self.config)])
        self.assertEqual(caught.exception.code, 2)

    def test_bad_target_in_the_environment_is_rejected(self):
        with mock.patch.dict(os.environ, {"AISIS_WATCHER_TARGETS": "no-port"}), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as caught:
                subprocess_main(["--config", str(self.config)])
        self.assertEqual(caught.exception.code, 2)


def subprocess_main(argv):
    with mock.patch.object(sys, "argv", ["backend_watcher.py", *argv]):
        return bw.main()


class EndToEnd(Base):
    def test_script_logs_timestamped_transitions_and_flips_the_file(self):
        proc = subprocess.Popen(
            [sys.executable, "-u", str(HERE / "backend_watcher.py"), "--config", str(self.config),
             "--target", f"127.0.0.1:{self.a.port}", "--target", f"127.0.0.1:{self.b.port}",
             "--interval", "0.1", "--timeout", "0.2"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        self.addCleanup(proc.kill)
        lines = []
        reader = threading.Thread(target=lambda: [lines.append(line.rstrip()) for line in proc.stdout])
        reader.start()

        def wait_for(value, seconds=6):
            deadline = time.time() + seconds
            while time.time() < deadline:
                if self.enabled() is value:
                    return True
                time.sleep(0.05)
            return False

        deadline = time.time() + 10
        while time.time() < deadline and not any("start:" in line for line in lines):
            time.sleep(0.05)  # the watcher must have taken its start-up probe before we break anything
        self.assertTrue(any("start: config enabled=true, targets all reachable" in line for line in lines), lines)
        self.assertTrue(self.enabled())
        self.a.stop()
        self.assertTrue(wait_for(False), lines)
        self.a.start()
        self.assertTrue(wait_for(True), lines)
        proc.terminate()
        proc.wait(5)
        reader.join()
        proc.stdout.close()
        transitions = [line for line in lines if "TRANSITION" in line]
        self.assertEqual(len(transitions), 2, lines)
        self.assertRegex(transitions[0], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d\d:\d\d backend-watcher: TRANSITION UP->DOWN")
        self.assertIn("TRANSITION DOWN->UP", transitions[1])
        print("\n    e2e log:\n      " + "\n      ".join(lines))


def dns_query(name, port):
    labels = b"".join(bytes([len(p)]) + p.encode() for p in name.split(".")) + b"\0"
    packet = struct.pack("!6H", 0x4242, 0x0100, 1, 0, 0, 0) + labels + struct.pack("!2H", 1, 1)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(3)
        sock.bind(("127.0.0.1", 0))
        sock.sendto(packet, ("127.0.0.1", port))
        data, _ = sock.recvfrom(512)
    count = struct.unpack("!H", data[6:8])[0]
    return socket.inet_ntoa(data[-4:]) if count else None


@unittest.skipUnless(Path(DNS_FORWARDER).exists() and UPSTREAM_IP, "needs DNS_FORWARDER and TEST_UPSTREAM_IP")
class RealRelay(Base):
    """The unmodified relay code against the temp config: dns_ready, and a live flip without any restart."""

    def free_port(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    def make_relay_config(self, port, arp, upstream_port):
        cfg = json.loads(CONFIG_TEXT)
        cfg.update(port=port, upstreams=[UPSTREAM_IP], upstream_port=upstream_port, arp_file=str(arp))
        self.config.write_text(json.dumps(cfg, indent=2) + "\n")
        self.config.chmod(0o600)

    def start_relay(self):
        proc = subprocess.Popen([sys.executable, "-u", DNS_FORWARDER, "--config", str(self.config)],
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        self.addCleanup(proc.kill)
        return proc, json.loads(proc.stdout.readline())

    def test_dns_ready_override_flag_and_live_flip(self):
        arp = self.dir / "arp"
        arp.write_text("IP address       HW type     Flags       HW address            Mask     Device\n"
                       "127.0.0.1        0x1         0x2         aa:bb:cc:dd:ee:ff     *        lo\n")
        # fake upstream DNS: answers every A query with 203.0.113.9
        up = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        up.bind((UPSTREAM_IP, 0))
        up.settimeout(0.2)
        self.addCleanup(up.close)
        stop = threading.Event()

        def serve():
            while not stop.is_set():
                try:
                    data, addr = up.recvfrom(512)
                except socket.timeout:
                    continue
                reply = data[:2] + struct.pack("!HHHHH", 0x8180, 1, 1, 0, 0) + data[12:] + \
                    b"\xc0\x0c" + struct.pack("!HHIH", 1, 1, 60, 4) + socket.inet_aton("203.0.113.9")
                up.sendto(reply, addr)

        thread = threading.Thread(target=serve)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(stop.set)
        port = self.free_port()
        self.make_relay_config(port, arp, up.getsockname()[1])

        # enabled=false at relay start -> dns_ready override_enabled false
        w = self.started()
        self.a.stop()
        self.step(w, 3)
        self.assertFalse(self.enabled())
        relay, ready = self.start_relay()
        print("\n    dns_ready with watcher-written enabled=false:", json.dumps(ready))
        self.assertIs(ready["override_enabled"], False)
        self.assertEqual(dns_query("quasar.yandex.net", port), "203.0.113.9", "DOWN: plain forwarding to the upstream")
        relay.kill()
        relay.wait()

        # enabled=true at relay start -> dns_ready override_enabled true; the override answers route_ip itself
        self.a.start()
        self.step(w, 3)
        self.assertTrue(self.enabled())
        relay, ready = self.start_relay()
        print("    dns_ready with watcher-written enabled=true: ", json.dumps(ready))
        self.assertIs(ready["override_enabled"], True)
        self.assertEqual(dns_query("quasar.yandex.net", port), "127.0.0.1", "UP: override answers route_ip")
        # live flip with the SAME relay process: per-request re-read, no restart
        self.a.stop()
        self.step(w, 3)
        self.assertEqual(dns_query("quasar.yandex.net", port), "203.0.113.9", "flip to DOWN seen without restart")
        self.a.start()
        self.step(w, 3)
        self.assertEqual(dns_query("quasar.yandex.net", port), "127.0.0.1", "flip back to UP seen without restart")
        print("    same relay process: DOWN -> 203.0.113.9 (forwarded), UP -> 127.0.0.1 (override), no restart")


if __name__ == "__main__":
    unittest.main()

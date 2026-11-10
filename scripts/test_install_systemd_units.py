"""Tests for install-systemd-units.sh: run with `python3 -m unittest discover -s scripts`.

The script runs against a throwaway HOME and a stand-in `systemctl` that keeps its state in a directory: it records
every call, writes the .wants links `enable` would write, and fails `start` for the units named in $FAIL_START.
Nothing touches the real systemd or the real ~/.config.
"""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / "install-systemd-units.sh"
UNITS = Path(__file__).resolve().parents[1] / "services" / "yandex-station" / "systemd"

FAKE_SYSTEMCTL = """#!/usr/bin/env bash
shift  # --user
state="$FAKE_STATE"; unit_dir="$XDG_CONFIG_HOME/systemd/user"
echo "$*" >> "$state/calls"
case "$1" in
  show-environment) exit 0 ;;
  daemon-reload) [ -e "$state/reload-fails" ] && exit 1; exit 0 ;;
  show) for last; do :; done; [ -e "$unit_dir/$last" ] && echo "$unit_dir/$last"; exit 0 ;;
  is-active) [ -e "$state/active-$2" ] && { echo active; exit 0; }; echo inactive; exit 3 ;;
  start)
    case " $FAIL_START " in *" $2 "*) exit 1 ;; esac
    case " $SKIP_START " in *" $2 "*) exit 0 ;; esac
    : > "$state/active-$2" ;;
  enable)
    [ "$2" = --force ] && { set -- "$1" "$3" --force; }
    for target in $(awk '/^\\[/{i=($0=="[Install]");next} i&&/^WantedBy=/{sub(/^WantedBy=/,"");print}' "$unit_dir/$2"); do
      mkdir -p "$unit_dir/$target.wants"; link="$unit_dir/$target.wants/$2"
      if [ -L "$link" ] && [ "$(readlink "$link")" != "$unit_dir/$2" ] && [ "$3" != --force ]; then exit 1; fi   # a conflicting link needs --force, as in systemd
      ln -sf "$unit_dir/$2" "$link"
    done ;;
esac
"""


class InstallerTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.home = self.root / "home"
        self.state = self.root / "state"
        bin_dir = self.root / "bin"
        for directory in (self.home, self.state, bin_dir):
            directory.mkdir()
        stub = bin_dir / "systemctl"
        stub.write_text(FAKE_SYSTEMCTL)
        stub.chmod(0o755)
        self.bin_dir = bin_dir
        self.dest = self.home / ".config" / "systemd" / "user"

    def run_script(self, *args, fail_start="", skip_start=""):
        env = {**os.environ, "HOME": str(self.home), "XDG_CONFIG_HOME": str(self.home / ".config"),
               "PATH": f"{self.bin_dir}:{os.environ['PATH']}", "FAKE_STATE": str(self.state), "FAIL_START": fail_start, "SKIP_START": skip_start}
        return subprocess.run(["bash", str(SCRIPT), *args], capture_output=True, text=True, env=env)

    def calls(self):
        path = self.state / "calls"
        return path.read_text().splitlines() if path.exists() else []

    def enabled_units(self):
        return {p.name for p in self.dest.glob("*.wants/*")}

    def test_check_on_an_empty_home_reports_drift_and_writes_nothing(self):
        result = self.run_script("--check")
        self.assertEqual(result.returncode, 3)
        self.assertIn("missing:", result.stdout)
        self.assertIn("not enabled: aisis-station-adapter.service", result.stdout)
        self.assertFalse(self.dest.exists())
        self.assertEqual([c for c in self.calls() if c.split()[0] not in ("show-environment", "show", "is-active")], [])

    def test_install_copies_enables_and_starts_every_unit_then_checks_clean(self):
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        for path in UNITS.rglob("*"):
            if path.is_file():
                self.assertEqual((self.dest / path.relative_to(UNITS)).read_bytes(), path.read_bytes())
        self.assertIn("wyoming-whisper.service", self.enabled_units())
        self.assertIn("aisis-whisper-prompt.timer", self.enabled_units())
        self.assertNotIn("aisis-whisper-prompt.service", self.enabled_units())  # oneshot without [Install]
        self.assertEqual(self.run_script("--check").returncode, 0)

    def test_check_reports_an_enabled_unit_that_is_not_running_until_it_runs(self):
        self.run_script(fail_start="aisis-ha-cli.service")
        result = self.run_script("--check")
        self.assertEqual(result.returncode, 3)
        self.assertIn("not active: aisis-ha-cli.service", result.stdout)
        self.assertEqual(self.run_script().returncode, 0)   # the next apply starts it
        self.assertEqual(self.run_script("--check").returncode, 0)

    def test_a_start_that_succeeds_without_activating_the_unit_is_a_failure(self):
        result = self.run_script(skip_start="aisis-dashboard.service")
        self.assertEqual(result.returncode, 10)
        self.assertIn("aisis-dashboard.service", result.stderr)
        self.assertNotIn("started: aisis-dashboard.service", result.stdout)
        self.assertEqual(self.run_script("--check").returncode, 3)

    def test_check_without_a_user_session_does_not_claim_the_units_run(self):
        self.run_script()
        (self.bin_dir / "systemctl").write_text("#!/usr/bin/env bash\nexit 1\n")
        result = self.run_script("--check")
        self.assertEqual(result.returncode, 0)
        self.assertIn("not verified running", result.stdout)

    def test_no_start_does_not_hide_a_stopped_unit_from_check(self):
        self.run_script(fail_start="aisis-ha-cli.service")
        result = self.run_script("--check", "--no-start")
        self.assertEqual(result.returncode, 3)
        self.assertIn("not active: aisis-ha-cli.service", result.stdout)

    def test_a_masked_enable_link_and_a_loose_file_mode_are_drift_and_get_repaired(self):
        self.run_script()
        link = self.dest / "default.target.wants" / "aisis-ha-cli.service"
        link.unlink()
        link.symlink_to("/dev/null")
        (self.dest / "aisis-dashboard.service").chmod(0o666)
        check = self.run_script("--check")
        self.assertEqual(check.returncode, 3)
        self.assertIn("not enabled: aisis-ha-cli.service", check.stdout)
        self.assertIn("mode:", check.stdout)
        repaired = self.run_script()
        self.assertEqual(repaired.returncode, 0)
        self.assertNotIn("restart to apply", repaired.stdout)   # same content: permissions only
        self.assertEqual((self.dest / "aisis-dashboard.service").stat().st_mode & 0o777, 0o644)
        self.assertEqual(self.run_script("--check").returncode, 0)

    def test_second_run_installs_and_restarts_nothing_but_still_reloads(self):
        self.run_script()
        (self.state / "calls").unlink()
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertEqual([c for c in self.calls() if c.split()[0] in ("start", "stop", "restart", "enable")], [])
        self.assertIn("daemon-reload", self.calls())

    def test_failed_start_still_enables_and_starts_the_rest_and_needs_manual(self):
        result = self.run_script(fail_start="aisis-station-adapter.service")
        self.assertEqual(result.returncode, 10)
        self.assertIn("aisis-station-adapter.service", result.stderr)
        self.assertIn("aisis-station-adapter.service", self.enabled_units())
        self.assertIn("wyoming-whisper.service", self.enabled_units())
        self.assertTrue((self.state / "active-wyoming-whisper.service").exists())
        self.assertTrue((self.state / "active-aisis-whisper-prompt.timer").exists())

    def test_reload_is_retried_after_a_failed_run_even_when_files_already_match(self):
        (self.state / "reload-fails").touch()
        self.assertNotEqual(self.run_script().returncode, 0)
        (self.state / "reload-fails").unlink()
        (self.state / "calls").unlink()
        self.assertEqual(self.run_script().returncode, 0)
        self.assertIn("daemon-reload", self.calls())

    def test_a_running_unit_whose_file_changed_is_named_not_restarted(self):
        self.run_script()
        (self.dest / "aisis-ha-cli.service").write_text("[Unit]\nDescription=stale\n")
        (self.state / "calls").unlink()
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("restart to apply: aisis-ha-cli.service", result.stdout)
        self.assertEqual([c for c in self.calls() if c.split()[0] in ("stop", "restart")], [])

    def test_check_reports_a_drifted_file_and_exits_3(self):
        self.run_script()
        with (self.dest / "aisis-ha-cli.service").open("a") as handle:
            handle.write("# local edit\n")
        result = self.run_script("--check")
        self.assertEqual(result.returncode, 3)
        self.assertIn("differs:", result.stdout)

    def test_symlinked_dropin_directory_and_unit_file_become_real_copies(self):
        checkout = self.root / "old-checkout"
        (checkout / "wyoming-whisper.service.d").mkdir(parents=True)
        for path in (UNITS / "wyoming-whisper.service.d").iterdir():
            (checkout / "wyoming-whisper.service.d" / path.name).write_bytes(path.read_bytes())
        self.dest.mkdir(parents=True)
        (self.dest / "wyoming-whisper.service.d").symlink_to(checkout / "wyoming-whisper.service.d")
        (self.dest / "aisis-ha-cli.service").symlink_to(UNITS / "aisis-ha-cli.service")
        check = self.run_script("--check")
        self.assertIn("symlink:", check.stdout)
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.dest / "wyoming-whisper.service.d").is_symlink())
        self.assertFalse((self.dest / "aisis-ha-cli.service").is_symlink())
        for path in (UNITS / "wyoming-whisper.service.d").iterdir():
            self.assertEqual((self.dest / "wyoming-whisper.service.d" / path.name).read_bytes(), path.read_bytes())
        self.assertEqual(self.run_script("--check").returncode, 0)

    def test_dead_dropin_link_is_replaced_and_nothing_is_written_through_a_live_one(self):
        self.dest.mkdir(parents=True)
        (self.dest / "wyoming-whisper.service.d").symlink_to(self.root / "gone")
        checkout = self.root / "live-checkout"
        (checkout / "x").mkdir(parents=True)
        (checkout / "x" / "dangling").symlink_to(self.root / "nowhere")
        (self.dest / "aisis-dashboard.service.d").symlink_to(checkout / "x")
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        for name in ("wyoming-whisper.service.d", "aisis-dashboard.service.d"):
            self.assertFalse((self.dest / name).is_symlink(), name)
        self.assertEqual(sorted(p.name for p in (checkout / "x").iterdir()), ["dangling"])
        self.assertEqual(self.run_script("--check").returncode, 0)

    def test_restart_notice_is_printed_even_when_the_reload_fails(self):
        self.run_script()
        (self.dest / "aisis-ha-cli.service").write_text("[Unit]\nDescription=stale\n")
        (self.state / "reload-fails").touch()
        result = self.run_script()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("restart to apply: aisis-ha-cli.service", result.stdout)

    def test_no_user_session_needs_manual(self):
        (self.bin_dir / "systemctl").write_text("#!/usr/bin/env bash\nexit 1\n")
        result = self.run_script()
        self.assertEqual(result.returncode, 10)
        self.assertIn("needs manual", result.stderr)


if __name__ == "__main__":
    unittest.main()

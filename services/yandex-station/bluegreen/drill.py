#!/usr/bin/env python3
"""Failover drill: kill the active color of an env and measure what a client sees.

  drill.py stage stop     # docker stop      (process gone, port refuses)
  drill.py stage kill     # docker kill      (SIGKILL; docker's restart policy also kicks in)
  drill.py stage pause    # docker pause     (hung: port accepts, nothing answers)

On stage it starts its own failover.py (no components, no alerts); on prod it relies on the
running aisis-failover.service (alerts included). Then it runs two synthetic clients through the switch's canonical ports:
  * a prober that opens a fresh /uni.ws session and GETs /devices every 100 ms;
  * a "speaker": one long-lived /uni.ws session that reconnects every 250 ms after a drop.
Prints one JSON summary. On prod it never sends anything but WebSocket handshakes
with throwaway device ids, so no device action is possible; the fault itself does
flap the real speaker session, so prod runs are deliberate (`--i-know-prod`).
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bgctl  # noqa: E402
import smoke  # noqa: E402


class Drill:
    def __init__(self, env: str) -> None:
        self.env = env
        self.host = "127.0.0.1"
        self.uni, self.api, _ = bgctl.ENVS[env]["switch"]
        self.stop = threading.Event()
        self.probes: list[tuple[float, bool]] = []
        self.session_drop: float | None = None
        self.session_back: float | None = None
        self.session_up = threading.Event()

    def prober(self) -> None:
        while not self.stop.is_set():
            started = time.time()
            ok = False
            try:
                with smoke.ws_open(self.host, self.uni, timeout=1.0):
                    ok = smoke.http_status(f"http://{self.host}:{self.api}/devices", 1.0) == 200
            except OSError:
                pass
            self.probes.append((started, ok))
            time.sleep(0.1)

    def speaker(self) -> None:
        sock = smoke.ws_open(self.host, self.uni, timeout=2.0)
        self.session_up.set()
        sock.settimeout(0.5)
        while not self.stop.is_set():
            try:
                if not sock.recv(4096):
                    break
            except TimeoutError:
                continue
            except OSError:
                break
        sock.close()
        if self.stop.is_set():
            return
        self.session_drop = time.time()
        while not self.stop.is_set():
            try:
                smoke.ws_open(self.host, self.uni, timeout=1.5).close()
                self.session_back = time.time()
                return
            except OSError:
                time.sleep(0.25)


def read_events(path: Path, offset: int) -> list[dict]:
    if not path.exists():
        return []
    with open(path) as handle:
        handle.seek(offset)
        return [json.loads(line) for line in handle if line.strip()]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("env", choices=list(bgctl.ENVS))
    parser.add_argument("mode", choices=("stop", "kill", "pause"))
    parser.add_argument("--i-know-prod", action="store_true")
    parser.add_argument("--settle", type=float, default=12.0, help="seconds to keep observing after the fault")
    args = parser.parse_args()
    env = args.env
    if env == "prod" and not args.i_know_prod:
        print("refusing to fault prod without --i-know-prod", file=sys.stderr)
        return 2

    active, standby = bgctl.active_color(env), bgctl.other(bgctl.active_color(env))
    for color in (active, standby):
        if not bgctl.health(env, color)["ok"]:
            print(f"precondition: {env} {color} is not healthy", file=sys.stderr)
            return 2

    events_path = bgctl.STATE_DIR / "failover-events.jsonl"
    offset = events_path.stat().st_size if events_path.exists() else 0
    watchdog = None
    if env == "stage":
        watchdog = subprocess.Popen([sys.executable, str(Path(__file__).resolve().parent / "failover.py"),
                                     "--env", env, "--no-components", "--no-alert"], stdout=subprocess.DEVNULL)
    elif subprocess.run(["systemctl", "--user", "is-active", "--quiet", "aisis-failover.service"]).returncode != 0:
        print("prod drill needs the running aisis-failover.service", file=sys.stderr)
        return 2
    drill = Drill(env)
    threads = [threading.Thread(target=drill.prober, daemon=True), threading.Thread(target=drill.speaker, daemon=True)]
    try:
        time.sleep(3.0)  # watchdog warm-up: it must be observing before the fault
        for thread in threads:
            thread.start()
        if not drill.session_up.wait(5):
            print("synthetic speaker could not connect", file=sys.stderr)
            return 2
        time.sleep(1.0)

        name = bgctl.container(env, active)
        t0 = time.time()
        fault = {"stop": ["stop", "-t", "0", name], "kill": ["kill", name], "pause": ["pause", name]}[args.mode]
        bgctl._docker(*fault, timeout=30)
        time.sleep(args.settle)
        # Wait for the failed color to be repaired as warm standby (bounded).
        repaired = None
        deadline = time.time() + 60
        while time.time() < deadline:
            if bgctl.health(env, active)["ok"]:
                repaired = time.time()
                break
            time.sleep(0.5)
    finally:
        drill.stop.set()
        for thread in threads:
            thread.join(timeout=3)
        if watchdog:
            watchdog.terminate()
            watchdog.wait(timeout=10)

    after = [(t, ok) for t, ok in drill.probes if t >= t0 - 0.05]
    failures = [t for t, ok in after if not ok]
    restored = None
    if failures:
        last_fail = max(failures)
        later_ok = [t for t, ok in after if ok and t > last_fail]
        restored = later_ok[0] if later_ok else None
    events = read_events(events_path, offset)
    failover_event = next((e for e in events if e["event"] == "failover"), None)
    summary = {
        "env": env,
        "mode": args.mode,
        "faulted": f"{active} (active) -> standby {standby}",
        "active_after": bgctl.active_color(env),
        "new_session_outage_s": None if failures and restored is None else round((restored - t0) if restored else 0.0, 2),
        "failed_probes": len(failures),
        "total_probes": len(after),
        "speaker_session_dropped_after_s": round(drill.session_drop - t0, 2) if drill.session_drop else None,
        "speaker_reconnected_after_s": round(drill.session_back - t0, 2) if drill.session_back else None,
        "watchdog_flip_after_s": round(failover_event["ts"] - t0, 2) if failover_event else None,
        "watchdog_flip_detail": failover_event,
        "failed_color_back_as_standby_after_s": round(repaired - t0, 1) if repaired else None,
        "restarts": [e for e in events if e["event"].startswith("restart_")],
    }
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())

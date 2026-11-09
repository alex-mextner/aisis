#!/usr/bin/env python3
"""Autofailover watchdog for the voice stack. Standard library only.

Every tick (default 1.5 s) it probes, in parallel:
  * the active and the standby quasar color (API /devices 200 + uniproxy port answers),
  * the switch (end to end, through its canonical ports),
  * with --components (prod default): adapter, stt, pm, ha-cli, postgres, tls-voice,
    dns-relay, kit (sync helper on :18080), lan-proxy, embed sidecar, wyoming tunnel.

A quasar color or the switch counts as failed after `--threshold` consecutive failed ticks
(default 3); other components after `--component-threshold` (default 6).
Then:
  * active color failed, standby healthy  -> flip the switch to the standby (nginx
    reload), then restart the failed color so it becomes the new warm standby;
  * active failed and standby failed too  -> restart the active color (no flip);
  * standby failed                        -> restart it (keeps rollback warm);
  * switch failed while the active color is healthy -> restart the switch container;
  * any other component                   -> restart its systemd user unit / container.
nginx fails over connects to a dead color by itself in milliseconds; this loop
covers the cases nginx cannot see (a hung process that still accepts connections)
and repairs whatever died.

One Telegram alert is sent when an incident opens and one when it recovers; every
probe transition and action goes to the journal and to state/failover-events.jsonl.
Actions are suspended while state/maintenance is fresh (deploy.sh sets it).

  failover.py --env prod            # the systemd unit aisis-failover.service
  failover.py --env stage --no-components --no-alert   # drills
"""
from __future__ import annotations

import argparse
import json
import socket
import subprocess
import sys
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bgctl  # noqa: E402
import smoke  # noqa: E402
import speaker_clock  # noqa: E402

CLOCK_CHECK_EVERY_S = 300.0

MAINTENANCE_MAX_AGE_S = 900.0
RESTART_COOLDOWN_S = 30.0


@dataclass(frozen=True)
class Component:
    name: str
    kind: str  # http | unit | container
    target: str  # url | unit name | container name
    unit: str | None = None  # systemd unit to restart when kind == http
    container: str | None = None  # container to restart when kind == http|container
    tcp_port: int | None = None  # extra TCP check on the container's published host IP
    every: int = 1  # probe on every Nth tick only (kit: its service logs each probe; others: subprocess cost)


COMPONENTS = (
    Component("adapter", "http", "http://127.0.0.1:31117/healthz", unit="aisis-station-adapter.service"),
    Component("stt", "http", "http://127.0.0.1:31121/healthz", unit="aisis-station-stt.service"),
    Component("pm", "http", "http://127.0.0.1:31118/health", unit="aisis-station-pm.service"),
    Component("ha-cli", "http", "http://127.0.0.1:8795/health", unit="aisis-ha-cli.service"),
    Component("postgres", "container", "aisis-yandex-postgres-1", container="aisis-yandex-postgres-1", every=3),
    Component("tls-voice", "container", "aisis-yandex-network-tls-voice-1",
              container="aisis-yandex-network-tls-voice-1", tcp_port=443, every=3),
    Component("dns-relay", "container", "aisis-yandex-network-dns-relay-1", container="aisis-yandex-network-dns-relay-1", every=3),
    # Speaker's quasar.yandex.net/.ru + scbh requests land here via tls-voice (kit.txt); sync info answers 200.
    Component("kit", "http", "http://192.168.0.56:18080/get_sync_info?device_id=aisis-failover-probe&platform=yandexmini_2",
              unit="aisis-station-kit.service", every=10),
    Component("lan-proxy", "unit", "aisis-station-lan-proxy.service", unit="aisis-station-lan-proxy.service", every=3),
    Component("embed", "unit", "aisis-station-embed.service", unit="aisis-station-embed.service", every=3),
)


@dataclass
class Tracker:
    fails: int = 0
    first_fail: float = 0.0
    incident: bool = False
    incident_start: float = 0.0
    last_action: float = 0.0
    detail: str = ""


@dataclass
class Watchdog:
    env: str
    interval: float
    threshold: int
    component_threshold: int
    http_timeout: float
    components: bool
    alert: bool
    state_dir: Path
    trackers: dict[str, Tracker] = field(default_factory=dict)
    ticks: int = 0
    clock: Future | None = None
    next_clock: float = 0.0
    log_lock: threading.Lock = field(default_factory=threading.Lock)

    # ── output ───────────────────────────────────────────────────────────
    def event(self, kind: str, **fields) -> None:
        record = {"ts": round(time.time(), 3), "env": self.env, "event": kind, **fields}
        line = json.dumps(record, ensure_ascii=False)
        with self.log_lock:
            print(line, flush=True)
            try:
                with open(self.state_dir / "failover-events.jsonl", "a") as handle:
                    handle.write(line + "\n")
            except OSError:
                pass

    def notify(self, text: str) -> None:
        if not self.alert:
            return
        try:
            subprocess.Popen(["tg", "--subagent", "aisis-failover", text], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
        except OSError as exc:
            self.event("alert_failed", error=str(exc))

    # ── probes ───────────────────────────────────────────────────────────
    def probe_component(self, comp: Component) -> tuple[bool, str]:
        if comp.kind == "http":
            status = smoke.http_status(comp.target, self.http_timeout)
            return status == 200, f"http {status}"
        if comp.kind == "unit":
            result = subprocess.run(["systemctl", "--user", "is-active", comp.target], capture_output=True, text=True)
            state = result.stdout.strip()
            return state == "active", state
        running = bgctl.container_running(comp.target)
        if not running:
            return False, "not running"
        if comp.tcp_port:
            host = self._published_host(comp)
            if host is None:
                return False, "no published port"
            try:
                with socket.create_connection((host, comp.tcp_port), timeout=self.http_timeout):
                    pass
            except OSError as exc:
                return False, f"tcp {host}:{comp.tcp_port} {type(exc).__name__}"
        return True, "running"

    @staticmethod
    def _published_host(comp: Component) -> str | None:
        result = bgctl._docker("port", comp.target, f"{comp.tcp_port}/tcp", timeout=5)
        line = result.stdout.strip().splitlines()[0] if result.returncode == 0 and result.stdout.strip() else ""
        host = line.rpartition(":")[0]
        return host.replace("0.0.0.0", "127.0.0.1") or None

    # ── actions ──────────────────────────────────────────────────────────
    def maintenance(self) -> bool:
        flag = self.state_dir / "maintenance"
        try:
            return time.time() - flag.stat().st_mtime < MAINTENANCE_MAX_AGE_S
        except OSError:
            return False

    def _cooldown_ok(self, key: str) -> bool:
        tracker = self.trackers[key]
        if time.time() - tracker.last_action < RESTART_COOLDOWN_S:
            return False
        tracker.last_action = time.time()
        return True

    def restart_container(self, name: str, key: str) -> None:
        if not self._cooldown_ok(key):
            return

        def work() -> None:
            started = time.monotonic()
            bgctl._docker("unpause", name, timeout=10)
            result = bgctl._docker("restart", "-t", "3", name, timeout=90)
            self.event("restart_container", name=name, ok=result.returncode == 0,
                       seconds=round(time.monotonic() - started, 2), error=result.stderr.strip()[-200:])

        threading.Thread(target=work, daemon=True).start()

    def restart_unit(self, unit: str, key: str) -> None:
        if not self._cooldown_ok(key):
            return

        def work() -> None:
            started = time.monotonic()
            result = subprocess.run(["systemctl", "--user", "restart", unit], capture_output=True, text=True, timeout=60)
            self.event("restart_unit", unit=unit, ok=result.returncode == 0,
                       seconds=round(time.monotonic() - started, 2), error=result.stderr.strip()[-200:])

        threading.Thread(target=work, daemon=True).start()

    # ── state machine ────────────────────────────────────────────────────
    def observe(self, key: str, ok: bool, detail: str) -> Tracker:
        tracker = self.trackers.setdefault(key, Tracker())
        tracker.detail = detail
        if ok:
            if tracker.incident:
                took = time.time() - tracker.incident_start
                self.event("recovered", target=key, outage_s=round(took, 1))
                self.notify(f"aisis-failover [{self.env}]: {key} восстановлен (инцидент {took:.0f} с)")
            tracker.fails, tracker.incident = 0, False
        else:
            if tracker.fails == 0:
                tracker.first_fail = time.time()
                self.event("probe_failed", target=key, detail=detail)
            tracker.fails += 1
        return tracker

    def open_incident(self, key: str, tracker: Tracker, message: str) -> None:
        if tracker.incident:
            return
        tracker.incident, tracker.incident_start = True, tracker.first_fail
        self.event("incident", target=key, detail=tracker.detail, message=message)
        self.notify(f"aisis-failover [{self.env}]: {message}")

    def failed(self, tracker: Tracker) -> bool:
        return tracker.fails >= self.threshold

    def tick(self, pool: ThreadPoolExecutor) -> None:
        env = self.env
        active = bgctl.active_color(env)
        standby = bgctl.other(active)
        jobs = {
            f"quasar-{active}": pool.submit(bgctl.health, env, active, self.http_timeout),
            f"quasar-{standby}": pool.submit(bgctl.health, env, standby, self.http_timeout),
            "switch": pool.submit(self._probe_switch),
        }
        if self.components:
            for comp in COMPONENTS:
                if self.ticks % comp.every == 0:
                    jobs[comp.name] = pool.submit(self.probe_component, comp)
        self.ticks += 1
        if self.components and self.clock is None and time.monotonic() >= self.next_clock:
            self.next_clock = time.monotonic() + CLOCK_CHECK_EVERY_S
            self.clock = pool.submit(speaker_clock.probe)
        if self.clock is not None and self.clock.done():
            finished, self.clock = self.clock, None
            self.check_speaker_clock(finished)
        results = {key: job.result() for key, job in jobs.items()}

        active_h, standby_h = results[f"quasar-{active}"], results[f"quasar-{standby}"]
        t_active = self.observe(f"quasar-{active}", active_h["ok"], json.dumps(active_h))
        t_standby = self.observe(f"quasar-{standby}", standby_h["ok"], json.dumps(standby_h))
        switch_ok, switch_detail = results["switch"]
        t_switch = self.observe("switch", switch_ok or not active_h["ok"], switch_detail)
        quiet = self.maintenance()

        if self.failed(t_active) and not quiet:
            self._handle_active_failure(active, standby, t_active, t_standby, standby_h["ok"])
        elif self.failed(t_standby) and not quiet:
            self.open_incident(f"quasar-{standby}", t_standby,
                               f"standby {standby} не отвечает ({t_standby.detail}), перезапускаю; active {active} жив")
            self.restart_container(bgctl.container(env, standby), f"quasar-{standby}")
        if self.failed(t_switch) and not quiet:
            self.open_incident("switch", t_switch, f"switch не отвечает ({t_switch.detail}), перезапускаю")
            self.restart_container(bgctl.ENVS[env]["switch_container"], "switch")

        if not self.components:
            return
        for comp in COMPONENTS:
            if comp.name not in jobs:
                continue  # not probed this tick: only fresh probes count toward the threshold
            ok, detail = results[comp.name]
            tracker = self.observe(comp.name, ok, detail)
            # Threshold is in time (component_threshold ticks); slower probes need fewer failures.
            needed = max(1 if comp.every == 1 else 2, -(-self.component_threshold // comp.every))
            if tracker.fails < needed or quiet:
                continue
            self.open_incident(comp.name, tracker, f"{comp.name} не отвечает ({detail}), перезапускаю")
            if comp.unit:
                self.restart_unit(comp.unit, comp.name)
            elif comp.container:
                self.restart_container(comp.container, comp.name)

    def check_speaker_clock(self, job: Future) -> None:
        """Collect a finished clock probe: alert once per incident, recovery alert comes from observe()."""
        ok, detail = job.result()
        if ok is None:
            self.event("speaker_clock_unknown", detail=detail)
            return
        tracker = self.observe("speaker-clock", ok, detail)
        if tracker.fails:
            self.open_incident("speaker-clock", tracker, f"часы колонки: {detail}")

    def _probe_switch(self) -> tuple[bool, str]:
        health = bgctl.switch_health(self.env, self.http_timeout)
        running = bgctl.container_running(bgctl.ENVS[self.env]["switch_container"])
        return bool(health["ok"] and running), json.dumps({**health, "running": running})

    def _handle_active_failure(self, active: str, standby: str, t_active: Tracker, t_standby: Tracker,
                               standby_ok: bool) -> None:
        key = f"quasar-{active}"
        if standby_ok:
            detected = time.time()
            try:
                old, new, took = bgctl.flip(self.env, standby)
            except RuntimeError as exc:
                self.event("flip_failed", error=str(exc))
                self.restart_container(bgctl.container(self.env, active), key)
                return
            self.event("failover", target=key, to=new, detect_s=round(detected - t_active.first_fail, 2),
                       flip_s=round(took, 3), since_first_fail_s=round(time.time() - t_active.first_fail, 2))
            self.open_incident(key, t_active, f"{old} упал ({t_active.detail}); трафик переключён на {new} "
                               f"за {time.time() - t_active.first_fail:.1f} с, перезапускаю {old} как standby")
            self.restart_container(bgctl.container(self.env, old), key)
            return
        self.open_incident(key, t_active, f"active {active} и standby {standby} оба не отвечают; перезапускаю {active}")
        self.restart_container(bgctl.container(self.env, active), key)
        if self.failed(t_standby):
            self.restart_container(bgctl.container(self.env, standby), f"quasar-{standby}")

    def run(self) -> None:
        self.event("start", interval=self.interval, threshold=self.threshold, components=self.components,
                   active=bgctl.active_color(self.env))
        with ThreadPoolExecutor(max_workers=16) as pool:
            while True:
                started = time.monotonic()
                try:
                    self.tick(pool)
                except Exception as exc:  # the watchdog itself must not die on a bad tick
                    self.event("tick_error", error=f"{type(exc).__name__}: {exc}")
                time.sleep(max(0.0, self.interval - (time.monotonic() - started)))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--env", choices=list(bgctl.ENVS), default="prod")
    parser.add_argument("--interval", type=float, default=1.5)
    parser.add_argument("--threshold", type=int, default=3)
    parser.add_argument("--component-threshold", type=int, default=6,
                        help="failed ticks before a non-quasar component is restarted (default 6 ≈ 9 s: "
                             "tolerates a manual `systemctl restart`)")
    parser.add_argument("--http-timeout", type=float, default=1.2)
    parser.add_argument("--components", action=argparse.BooleanOptionalAction, default=None,
                        help="also watch adapter/stt/ha-cli/... (default: on for prod, off for stage)")
    parser.add_argument("--alert", action=argparse.BooleanOptionalAction, default=None,
                        help="send Telegram alerts (default: on for prod, off for stage)")
    args = parser.parse_args()
    prod = args.env == "prod"
    bgctl.STATE_DIR.mkdir(parents=True, exist_ok=True)
    Watchdog(
        env=args.env,
        interval=args.interval,
        threshold=args.threshold,
        component_threshold=args.component_threshold,
        http_timeout=args.http_timeout,
        components=prod if args.components is None else args.components,
        alert=prod if args.alert is None else args.alert,
        state_dir=bgctl.STATE_DIR,
    ).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())

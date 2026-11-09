#!/usr/bin/env python3
"""aisis-ha-cli: always-on local Home Assistant proxy with state cache.

Serves 127.0.0.1:8790. Proxies REST calls to Home Assistant while it is up,
answers state reads from a persistent cache while it is down, and queues
service calls until HA returns. Keeps the voice stack (quasar container
ha-intents layer) working across HA restarts without its own restarts.

Endpoints (auth header optional; the real HA token is added here):
  GET  /health                 -> {ha_up, cache_age_s, cached_states, queued,
                                   recovery_eta_s, outages_measured}
  GET  /api/states             -> live, or cache with X-Aisis-Cache: stale
  GET  /api/states/<entity_id> -> live, or cache with X-Aisis-Cache: stale
  POST /api/template           -> live, or cached rendering with stale header
  POST /api/services/...       -> live, or 202 + queue (X-Aisis-Queue: 1,
                                   X-Aisis-Eta: <seconds>)
Anything else: proxied when HA is up, 503 otherwise.

Config: ~/.local/share/aisis/ha.config.json ({url, token}).
Cache:   ~/.local/share/aisis/ha-state-cache.json
Outages: ~/.local/share/aisis/ha-outages.json (measured down->up windows)
Env overrides for testing: AISIS_HACLI_HA_URL, AISIS_HACLI_PORT,
AISIS_HACLI_CACHE, AISIS_HACLI_OUTAGES.
"""
from __future__ import annotations

import json
import os
import sys
import statistics
import time
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BASE = Path.home() / '.local' / 'share' / 'aisis'
CONFIG_FILE = BASE / 'ha.config.json'
CACHE_FILE = Path(os.environ.get('AISIS_HACLI_CACHE', BASE / 'ha-state-cache.json'))
OUTAGES_FILE = Path(os.environ.get('AISIS_HACLI_OUTAGES', BASE / 'ha-outages.json'))
PORT = int(os.environ.get('AISIS_HACLI_PORT', '8795'))
REFRESH_S = 30.0
# While HA is down, probe fast so the measured outage ends within seconds of the
# real recovery and queued service calls replay promptly.
DOWN_POLL_S = 3.0
HA_TIMEOUT_S = 5.0
# Recovery time of a Home Assistant restart: median of the last OUTAGE_WINDOW
# down->up durations. Seeded with the 38 outages the ha-ble-vhci service logged
# ("Home Assistant is back after N s", journal 2026-10-23..11-07: median 62 s,
# nightly 03:00 restarts ~33 s, manual reloads 61-91 s, 6 outliers 145-434 s);
# real outages measured here are appended to OUTAGES_FILE and replace the seed
# oldest-first.
SEED_OUTAGES_S = [
    61, 37, 241, 91, 91, 33, 151, 91, 34, 62, 33, 91, 62, 63, 61, 91, 61, 63, 91,
    13, 78, 434, 7, 179, 145, 33, 269, 8, 17, 7, 6, 91, 91, 61, 12, 91, 91, 1,
]
OUTAGE_WINDOW = 40

_lock = threading.Lock()
_ha_up = False
_seen_up = False  # HA answered at least once since this daemon started
_down_since: float | None = None  # first failure of the current outage
_last_ok = 0.0
_queue: list[tuple[str, dict | None]] = []  # (path, body) service calls
_wake = threading.Event()  # set when a request sees HA down: probe now, then fast


def load_config() -> tuple[str, str | None]:
    url = os.environ.get('AISIS_HACLI_HA_URL')
    token = None
    if not url:
        cfg = json.loads(CONFIG_FILE.read_text())
        url = cfg['url']
        token = cfg.get('token')
    return url.rstrip('/'), token


HA_URL, HA_TOKEN = load_config()


def ha_request(path: str, body: dict | None = None) -> tuple[int, bytes, dict]:
    req = urllib.request.Request(HA_URL + path, method='GET' if body is None else 'POST')
    if HA_TOKEN:
        req.add_header('Authorization', f'Bearer {HA_TOKEN}')
    if body is not None:
        req.add_header('Content-Type', 'application/json')
        data = json.dumps(body).encode()
    else:
        data = None
    try:
        with urllib.request.urlopen(req, data, timeout=HA_TIMEOUT_S) as resp:
            return resp.status, resp.read(), dict(resp.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers)
    except Exception:
        return 0, b'', {}


def read_cache() -> dict:
    try:
        return json.loads(CACHE_FILE.read_text())
    except Exception:
        return {}


def _load_measured_outages() -> list[dict]:
    try:
        data = json.loads(OUTAGES_FILE.read_text())
    except (OSError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    return [o for o in data if isinstance(o, dict) and isinstance(o.get('s'), (int, float))]


_measured_outages = _load_measured_outages()[-OUTAGE_WINDOW:]


def recovery_eta_s() -> int:
    """Typical HA restart time: median of recent down->up durations, rounded to 5 s."""
    window = (SEED_OUTAGES_S + [o['s'] for o in _measured_outages])[-OUTAGE_WINDOW:]
    return max(5, int(round(statistics.median(window) / 5.0)) * 5)


def _record_outage(seconds: float) -> None:
    """Keep one measured down->up window (caller holds _lock)."""
    _measured_outages.append({'at': round(time.time()), 's': round(seconds)})
    del _measured_outages[:-OUTAGE_WINDOW]
    try:
        tmp = OUTAGES_FILE.with_suffix('.json.tmp')
        tmp.write_text(json.dumps(_measured_outages))
        tmp.replace(OUTAGES_FILE)
    except OSError:
        pass  # read-only stage mount: keep the in-memory window


def _note_ha(ok: bool) -> None:
    """Track HA reachability and time the down->up window (caller holds _lock).

    A window is only measured when its start was observed (HA seen up first), so a
    daemon started during an outage does not record a truncated duration.
    """
    global _ha_up, _last_ok, _down_since, _seen_up
    now = time.time()
    if ok:
        if _down_since is not None:
            _record_outage(now - _down_since)
            _down_since = None
        _ha_up = _seen_up = True
        _last_ok = now
    else:
        _ha_up = False
        if _seen_up and _down_since is None:
            _down_since = now


def report_down() -> None:
    """A proxied request found HA unreachable: start timing now, probe immediately."""
    with _lock:
        _note_ha(False)
    _wake.set()


def refresh_loop() -> None:
    while True:
        status, raw, _ = ha_request('/api/states')
        cache = {}
        if status == 200:
            try:
                states = json.loads(raw)
            except Exception:
                states = None
            if isinstance(states, list):
                cache['states'] = states
                # Areas template needed by the container registry builder.
                tstatus, traw, _ = ha_request('/api/template', {'template': AREA_TEMPLATE})
                if tstatus == 200:
                    cache['areas'] = traw.decode('utf-8', 'replace')
                cache['saved_at'] = time.time()
                tmp = CACHE_FILE.with_suffix('.json.tmp')
                tmp.write_text(json.dumps(cache, ensure_ascii=False))
                tmp.replace(CACHE_FILE)
        pending: list[tuple[str, dict | None]] = []
        with _lock:
            _note_ha(status == 200)
            if status == 200 and _queue:
                pending, _queue[:] = _queue[:], []
        for p, b in pending:
            ha_request(p, b)
        _wake.wait(REFRESH_S if _ha_up else DOWN_POLL_S)
        _wake.clear()


# Keep in sync with backend/ha-intents.ts AREA_TEMPLATE.
AREA_TEMPLATE = (
    '{% for e in states %}{% set d = device_id(e.entity_id) %}'
    '{% if d %}{{ e.entity_id }}\t{{ e.attributes.friendly_name }}\t'
    '{{ area_name(d) }}\n{% endif %}{% endfor %}'
)


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, fmt, *args):  # quiet
        pass

    def _send(self, status: int, body: bytes, headers: dict | None = None,
              content_type: str = 'application/json'):
        self.send_response(status)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split('?')[0]
        if path == '/health':
            with _lock:
                body = json.dumps({
                    'ha_up': _ha_up,
                    'cache_age_s': None if not _ha_up else round(time.time() - _last_ok),
                    'cached_states': len(read_cache().get('states', [])),
                    'queued': len(_queue),
                    'recovery_eta_s': recovery_eta_s(),
                    'outages_measured': len(_measured_outages),
                }).encode()
            self._send(200, body)
            return
        if path == '/api/states' or path.startswith('/api/states/'):
            status, raw, _ = ha_request(path)
            if status == 200:
                self._send(200, raw, {'X-Aisis-Cache': 'live'})
                return
            if status == 0:
                report_down()
            # HA down: serve cache.
            cache = read_cache()
            states = cache.get('states', [])
            if path == '/api/states':
                self._send(200, json.dumps(states).encode(), {'X-Aisis-Cache': 'stale'})
                return
            entity = path.rsplit('/', 1)[1]
            for s in states:
                if s.get('entity_id') == entity:
                    self._send(200, json.dumps(s).encode(), {'X-Aisis-Cache': 'stale'})
                    return
            self._send(404, b'{"error": "not in cache"}', {'X-Aisis-Cache': 'stale'})
            return
        # Other GETs: straight proxy.
        status, raw, headers = ha_request(path)
        if status:
            self._send(status, raw)
        else:
            self._send(503, b'{"error": "ha down"}')

    def do_POST(self):
        path = self.path.split('?')[0]
        length = int(self.headers.get('Content-Length') or 0)
        try:
            body = json.loads(self.rfile.read(length) or b'null')
        except Exception:
            body = None
        if path == '/api/template':
            status, raw, _ = ha_request(path, body)
            if status == 200:
                self._send(200, raw, {'X-Aisis-Cache': 'live'})
                return
            cache = read_cache()
            if cache.get('areas') is not None:
                self._send(200, cache['areas'].encode(), {'X-Aisis-Cache': 'stale'})
            else:
                self._send(503, b'{"error": "ha down, no cache"}')
            return
        if path.startswith('/api/services/'):
            status, raw, _ = ha_request(path, body)
            if status:
                self._send(status, raw)
                return
            report_down()
            with _lock:
                _queue.append((path, body))
                queued = len(_queue)
            eta = recovery_eta_s()
            self._send(202, json.dumps({
                'queued': True, 'position': queued, 'recovery_eta_s': eta,
            }).encode(), {'X-Aisis-Queue': '1', 'X-Aisis-Eta': str(eta)})
            return
        status, raw, _ = ha_request(path, body)
        if status:
            self._send(status, raw)
        else:
            self._send(503, b'{"error": "ha down"}')


def main() -> int:
    threading.Thread(target=refresh_loop, daemon=True).start()
    server = ThreadingHTTPServer(('127.0.0.1', PORT), Handler)
    print(f'aisis-ha-cli on 127.0.0.1:{PORT}, upstream {HA_URL}', file=sys.stderr)
    server.serve_forever()
    return 0


if __name__ == '__main__':
    sys.exit(main())

"""YSE (Yandex Station Extended) integration for the dashboard.

- static panel table (the only hosts/ports the reverse proxy may reach: no open proxy)
- reachability probes, the on-speaker package list (`yam-run list` over adb) and a cached
  view of the public registry (https://repo.yse.wtf/v2/index.json)
- reverse proxy helpers: header filtering and a shim that makes panels written with
  root-absolute URLs ('/api/...') work when served under '/yse/<id>/'
"""
from __future__ import annotations

import http.client
import json
import re
import socket
import subprocess
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

SPEAKER_HOST = "192.168.0.148"
TARGET = "yandexmini_2"
REGISTRY_URL = "https://repo.yse.wtf/v2/index.json"
REGISTRY_TTL_S = 600
PROBE_TIMEOUT_S = 1.5
PROXY_TIMEOUT_S = 10
MAX_HTML_BYTES = 5 * 1024 * 1024
MAX_REQUEST_BODY = 10 * 1024 * 1024

PANELS = (
    {"id": "wakeword", "name": "Слова активации", "desc": "tangenx/wakeword: свои слова активации", "port": 8780},
    {"id": "yam", "name": "YAM", "desc": "Веб-панель менеджера пакетов YAM", "port": 8099},
    {"id": "uwu-eyes", "name": "UwU eyes", "desc": "Анимированные глаза на светодиодах", "port": 1213},
    {"id": "ticker", "name": "Ticker", "desc": "Бегущая строка на светодиодах", "port": 1214},
)
PANEL_BY_ID = {p["id"]: p for p in PANELS}


# ---------- probes ----------
def probe_panel(panel: dict, host: str | None = None, timeout: float = PROBE_TIMEOUT_S) -> tuple[bool, str]:
    """(reachable, status text): TCP connect, then GET / and report the HTTP status."""
    host = host or SPEAKER_HOST
    try:
        conn = http.client.HTTPConnection(host, panel["port"], timeout=timeout)
        try:
            conn.request("GET", "/", headers={"Connection": "close"})
            resp = conn.getresponse()
            resp.read(256)
            return True, f"HTTP {resp.status}"
        finally:
            conn.close()
    except ConnectionRefusedError:
        return False, "порт закрыт (не запущено)"
    except (OSError, http.client.HTTPException) as e:
        return False, "нет ответа" if isinstance(e, socket.timeout) else str(e)[:80]


def panels_state(host: str | None = None) -> list[dict]:
    host = host or SPEAKER_HOST
    with ThreadPoolExecutor(max_workers=len(PANELS)) as pool:
        probes = list(pool.map(lambda p: probe_panel(p, host), PANELS))
    return [{"id": p["id"], "name": p["name"], "desc": p["desc"], "port": p["port"],
             "directUrl": f"http://{host}:{p['port']}/", "proxyUrl": f"/yse/{p['id']}/",
             "reachable": ok, "status": status} for p, (ok, status) in zip(PANELS, probes)]


# ---------- on-speaker packages ----------
def installed_packages(adb: str, timeout: float = 12) -> list[str]:
    """Lines of `/data/yam/bin/yam-run list` (best-effort: [] when adb/yam is unavailable)."""
    try:
        p = subprocess.run(["adb", "-s", adb, "shell", "/data/yam/bin/yam-run list"],
                           capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return []
    if p.returncode != 0:
        return []
    return [line.rstrip() for line in p.stdout.splitlines() if line.strip()]


# ---------- registry ----------
def parse_registry(data, target: str = TARGET) -> list[dict]:
    pkgs = data.get("packages") if isinstance(data, dict) else None
    if not isinstance(pkgs, list):
        raise ValueError("registry: нет packages")
    out = []
    for p in pkgs:
        if not isinstance(p, dict) or not isinstance(p.get("name"), str):
            continue
        targets = p.get("targets")
        out.append({"name": p["name"], "latest": p.get("latest"), "summary": p.get("summary") or "",
                    "compatible": isinstance(targets, list) and target in targets})
    return out


_registry = {"t": 0.0, "view": None}
_registry_lock = threading.Lock()


def fetch_registry_json(url: str = REGISTRY_URL, timeout: float = 10):
    # The CDN answers 403 to urllib's default User-Agent.
    req = urllib.request.Request(url, headers={"User-Agent": "aisis-dashboard/2.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def registry_view(fetch=fetch_registry_json, now=time.time) -> dict:
    """Registry summary cached for 10 minutes; on fetch failure the stale copy (or an empty one) + err."""
    with _registry_lock:
        cached = _registry["view"]
        if cached is not None and now() - _registry["t"] < REGISTRY_TTL_S:
            return cached
        try:
            packages = parse_registry(fetch())
        except Exception as e:
            err = str(e)[:120]
            if cached is not None:
                return {**cached, "err": err}
            return {"url": REGISTRY_URL, "count": 0, "fetchedAt": None, "packages": [], "err": err}
        view = {"url": REGISTRY_URL, "count": len(packages),
                "fetchedAt": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now())), "packages": packages}
        _registry.update(t=now(), view=view)
        return view


def yse_state(adb: str, adb_is_online: bool) -> dict:
    return {"adbOnline": adb_is_online, "panels": panels_state(),
            "installed": installed_packages(adb) if adb_is_online else [],
            "registry": registry_view()}


# ---------- reverse proxy ----------
_HEAD_RE = re.compile(rb"<head(?:\s[^>]*)?>", re.I)


def shim_script(panel_id: str) -> str:
    """JS that prefixes root-absolute URLs with /yse/<id> for fetch, XHR and EventSource."""
    return (
        "(function(){var P=" + json.dumps("/yse/" + panel_id) + ";"
        "function f(u){return typeof u==='string'&&u.charAt(0)==='/'&&u.charAt(1)!=='/'"
        "&&u.indexOf(P+'/')!==0&&u!==P?P+u:u}"
        "var of=window.fetch;if(of)window.fetch=function(i,o){"
        "if(typeof i==='string')i=f(i);"
        "else if(typeof Request!=='undefined'&&i instanceof Request){var u=new URL(i.url);"
        "if(u.origin===location.origin)i=new Request(f(u.pathname+u.search),i)}"
        "return of.call(this,i,o)};"
        "var ox=XMLHttpRequest.prototype.open;"
        "XMLHttpRequest.prototype.open=function(m,u){arguments[1]=f(String(u));return ox.apply(this,arguments)};"
        "var OE=window.EventSource;if(OE){var NE=function(u,c){return new OE(f(String(u)),c)};"
        "NE.prototype=OE.prototype;NE.CONNECTING=0;NE.OPEN=1;NE.CLOSED=2;window.EventSource=NE}"
        "})();"
    )


def inject_shim(html: bytes, panel_id: str) -> bytes:
    """Insert the shim right after <head>; without a <head> prepend it so it still runs first."""
    tag = b"<script>" + shim_script(panel_id).encode() + b"</script>"
    m = _HEAD_RE.search(html)
    if m:
        return html[:m.end()] + tag + html[m.end():]
    return tag + html


_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers",
        "transfer-encoding", "upgrade", "date", "server", "content-length", "content-encoding", "x-frame-options",
        "content-security-policy", "content-security-policy-report-only"}


def strip_frame_ancestors(csp: str) -> str:
    parts = [d.strip() for d in csp.split(";")]
    return "; ".join(d for d in parts if d and not d.lower().startswith("frame-ancestors"))


def response_headers(upstream_headers, panel_id: str) -> list[tuple[str, str]]:
    """Upstream headers minus hop-by-hop and frame blockers; root-absolute redirects get the prefix."""
    out = []
    for name, value in upstream_headers:
        low = name.lower()
        if low == "content-security-policy":
            stripped = strip_frame_ancestors(value)
            if stripped:
                out.append((name, stripped))
        elif low in _HOP:
            continue
        elif low == "location" and value.startswith("/") and not value.startswith("//"):
            out.append((name, f"/yse/{panel_id}{value}"))
        else:
            out.append((name, value))
    return out


def is_html(content_type: str | None) -> bool:
    return bool(content_type) and content_type.split(";")[0].strip().lower() == "text/html"


def split_proxy_path(path_qs: str):
    """'/yse/<id>/<rest>?q' -> (panel dict, '/<rest>?q'); rest None for '/yse/<id>' (caller redirects to the
    trailing-slash form); None for an unknown id; ValueError for a malformed path."""
    m = re.fullmatch(r"/yse/([^/?#]+)(/[^#]*)?", path_qs)
    if not m:
        raise ValueError("bad path")
    panel = PANEL_BY_ID.get(m.group(1))
    if panel is None:
        return None
    return panel, m.group(2)


def open_upstream(panel: dict, method: str, rest: str, body: bytes | None, content_type: str | None,
                  host: str | None = None):
    """Send the request to the panel; returns (connection, response). Caller closes the connection."""
    host = host or SPEAKER_HOST
    conn = http.client.HTTPConnection(host, panel["port"], timeout=PROXY_TIMEOUT_S)
    headers = {"Accept-Encoding": "identity", "Connection": "close", "Host": f"{host}:{panel['port']}"}
    if content_type:
        headers["Content-Type"] = content_type
    try:
        conn.request(method, rest, body=body, headers=headers)
        return conn, conn.getresponse()
    except BaseException:
        conn.close()
        raise

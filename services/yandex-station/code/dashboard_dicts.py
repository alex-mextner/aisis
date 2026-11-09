"""Dashboard «Словари»: JSON-ready wrappers over stt-cli's dictionary manager (stt_cli.dictmgmt).

There is ONE vocabulary store and it belongs to stt-cli (personal dictionary, installed marketplace
dictionaries with their overlays, profiles.json). The dashboard only edits it through dictmgmt, so
nothing here keeps user vocabulary. The module is independent of the HTTP handler: `route()` takes
(method, path, query, body) and returns (status, content type, text); dashboard.py just forwards.

stt_cli is imported lazily so the dashboard still starts when the package is missing: every route
then answers 503 {error: "stt_cli_unavailable", how: ...}. AISIS_STT_CLI_SRC (a dev checkout) is put
on sys.path first.

Mutating routes answer HTTP 200 {ok: false, err} for a diagnosed failure (the dashboard convention)
and 400/404/503 only for malformed requests, unknown routes and a missing package.
"""
import asyncio
import dataclasses
import inspect
import json
import os
import sys
from urllib.parse import unquote

HOW_INSTALL = ("установите stt-cli в окружение дашборда: pip install "
               "'git+https://git.hyperide.ai/ultrabricks/stt-cli' (для разработки: AISIS_STT_CLI_SRC=<путь к checkout>)")
PERSONAL = "personal"
FORMATS = ("json", "csv", "txt")
EXPORT_TYPES = {"json": "application/json; charset=utf-8", "csv": "text/csv; charset=utf-8",
                "txt": "text/plain; charset=utf-8"}


class Unavailable(Exception):
    """stt_cli cannot be imported."""


class BadRequest(Exception):
    """The request itself is malformed (HTTP 400)."""


def _stt():
    """(dictmgmt, error base class); Unavailable when stt-cli is not importable."""
    src = os.environ.get("AISIS_STT_CLI_SRC")
    if src and src not in sys.path:
        sys.path.insert(0, src)
    try:
        from stt_cli import _errors, dictmgmt
    except ImportError as e:
        raise Unavailable(str(e)) from e
    return dictmgmt, _errors.SttError


def _plain(obj):
    """Dataclass / namedtuple / tuple / set tree -> JSON-ready dicts and lists."""
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: _plain(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
    if isinstance(obj, dict):
        return {str(k): _plain(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set, frozenset)):
        return [_plain(v) for v in obj]
    return obj


def _first(d, *names, default=None):
    for n in names:
        if n in d and d[n] is not None:
            return d[n]
    return default


def _json(status, payload):
    return status, "application/json; charset=utf-8", json.dumps(payload, ensure_ascii=False)


def _ok(payload):
    return _json(200, {"ok": True, **payload})


def _fail(e, status=200):
    """An SttError (what / why / how) as one Russian-UI-friendly message plus its parts."""
    what, why, how = getattr(e, "what", str(e)), getattr(e, "why", ""), getattr(e, "how", "")
    err = what + (f" — {why}" if why else "") + (f". {how[:1].upper()}{how[1:]}" if how else "")
    return _json(status, {"ok": False, "err": err, "what": what, "why": why, "how": how,
                          "error": type(e).__name__})


def _unavailable(e):
    return _json(503, {"ok": False, "error": "stt_cli_unavailable", "err": f"stt-cli недоступен: {e}. {HOW_INSTALL[:1].upper()}{HOW_INSTALL[1:]}",
                       "how": HOW_INSTALL})


def reload_daemon():
    """Ask the warm `stt serve` daemon to re-read profiles/dictionaries; its absence is not an error."""
    try:
        from stt_cli.serve import client
        fn = getattr(client, "reload", None)
        if fn is None:
            return False
        result = fn()
        if inspect.isawaitable(result):
            asyncio.run(result)
        return True
    except Exception:
        return False


def _terms_of(raw):
    out = []
    for t in raw:
        t = _plain(t)
        if isinstance(t, str):
            t = {"term": t}
        term = str(t.get("term", ""))
        out.append({"id": str(_first(t, "id", default=term)), "term": term,
                    "aka": [str(a) for a in (t.get("aka") or [])], "note": str(t.get("note") or ""),
                    "state": str(_first(t, "state", default="personal")), "base": t.get("base")})
    return out


def _dictionary_row(d):
    d = _plain(d)
    source = str(_first(d, "id", "source", default=""))
    kind = _first(d, "kind", default=PERSONAL if source == PERSONAL else "installed")
    return {
        "source": source,
        "title": "Мой словарь" if kind == PERSONAL else str(_first(d, "title", "name", default=source)),
        "description": str(_first(d, "description", default="")),
        "kind": kind,
        "terms": int(_first(d, "term_count", "terms", "count", default=0) or 0),
        "version": _first(d, "version"),
        "editable": kind != "available",
        "has_overlay": bool(_first(d, "has_overlay", default=False)),
        "enabled": {str(p): bool(on) for p, on in (_first(d, "enabled", default={}) or {}).items()},
        "installed": kind != "available",
        "update_available": bool(_first(d, "update_available", default=False)),
        "warnings": [str(w) for w in (_first(d, "warnings", default=[]) or [])],
    }


def _sync_summary(status):
    s = _plain(status) if status is not None else {}
    updates = _first(s, "updates", default=[]) or []
    return {
        "online": bool(_first(s, "reachable", "online", default=False)),
        "checked_at": _first(s, "checked_at", "at"),
        "source": _first(s, "source"),
        "message": _first(s, "message"),
        "error": _first(s, "error", "err"),
        "updates": [str(u if isinstance(u, str) else _first(u, "id", default="")) for u in updates],
    }


def _marketplace_rows(status, known):
    """Marketplace entries the local listing does not have yet (the live catalog can be newer than the bundled one)."""
    s = _plain(status) if status is not None else {}
    rows = []
    for e in _first(s, "available", "catalog", default=[]) or []:
        if isinstance(e, str):
            e = {"id": e}
        ident = str(_first(e, "id", "source", default=""))
        if not ident or ident in known:
            continue
        rows.append({"source": ident, "title": str(_first(e, "title", "name", default=ident)),
                     "description": str(_first(e, "description", default="")), "kind": "available",
                     "terms": int(_first(e, "term_count", "terms", "count", default=0) or 0),
                     "version": _first(e, "version"), "editable": False, "has_overlay": False, "enabled": {},
                     "installed": False, "update_available": False, "warnings": []})
    return rows


def list_all(dm, refresh=False):
    try:
        status = dm.sync_status(refresh=True) if refresh else dm.sync_status()
    except Exception as e:  # marketplace trouble must not hide the local dictionaries
        status = {"reachable": False, "error": getattr(e, "what", str(e))}
    summary = _sync_summary(status)
    rows = [_dictionary_row(d) for d in dm.list_dictionaries()]
    for r in rows:
        r["update_available"] = r["update_available"] or r["source"] in summary["updates"]
    profiles = sorted({p for r in rows for p in r["enabled"]})
    return {"profiles": profiles, "dictionaries": rows + _marketplace_rows(status, {r["source"] for r in rows}),
            "sync": summary}


def _need(body, key, kind=str):
    v = body.get(key)
    if not isinstance(v, kind) or (kind is str and not v.strip()):
        raise BadRequest(f"поле «{key}» обязательно")
    return v


def _term_args(body):
    aka = body.get("aka") or []
    if not isinstance(aka, list) or not all(isinstance(a, str) for a in aka):
        raise BadRequest("«aka» должен быть списком строк")
    note = body.get("note") or ""
    if not isinstance(note, str):
        raise BadRequest("«note» должна быть строкой")
    return _need(body, "term").strip(), [a.strip() for a in aka if a.strip()], note


def _term_id(query, body):
    """The id of the term to change: the text for the personal dictionary, the catalog entry id for an installed one."""
    ident = (query.get("id") or [None])[0] or body.get("id")
    if not isinstance(ident, str) or not ident.strip():
        raise BadRequest("укажите термин: ?id=… или поле «id»")
    return ident


def _format(raw):
    if raw not in FORMATS:
        raise BadRequest(f"формат «{raw}» не поддерживается: {', '.join(FORMATS)}")
    return raw


def _handle(dm, method, parts, query, body):
    if parts == ["sync"] and method == "GET":
        refresh = (query.get("refresh") or ["0"])[0] in ("1", "true")
        s = dm.sync_status(refresh=True) if refresh else dm.sync_status()
        return _ok({"sync": _sync_summary(s)})
    if parts == ["sync", "install"] and method == "POST":
        ident = _need(body, "id")
        result = _plain(dm.sync_install(ident))
        reload_daemon()
        return _ok({"id": ident, "result": result})
    if parts == ["test"] and method == "POST":
        result = _plain(dm.test_phrase(_need(body, "profile"), _need(body, "text")))
        return _ok({"result": _test_view(result, body["text"])})
    if len(parts) == 3 and parts[0] == "profiles" and parts[2] == "enable" and method == "POST":
        on = body.get("on")
        if not isinstance(on, bool):
            raise BadRequest("«on» должно быть true или false")
        dm.enable(unquote(parts[1]), _need(body, "source"), on)
        reload_daemon()
        return _ok({"profile": parts[1], "source": body["source"], "on": on})
    if len(parts) == 2 and parts[1] == "export" and method == "GET":
        fmt = _format((query.get("fmt") or ["json"])[0])
        text = dm.export_terms(unquote(parts[0]), fmt)
        return 200, EXPORT_TYPES[fmt], text if isinstance(text, str) else json.dumps(_plain(text), ensure_ascii=False)
    if len(parts) == 2 and parts[1] == "import" and method == "POST":
        fmt = _format(body.get("fmt", "json"))
        payload = body.get("payload")
        if not isinstance(payload, str) or not payload.strip():
            raise BadRequest("поле «payload» (текст файла) обязательно")
        result = _plain(dm.import_terms(unquote(parts[0]), payload, fmt))
        reload_daemon()
        return _ok({"source": parts[0], "result": result})
    if len(parts) == 3 and parts[1:] == ["terms", "restore"] and method == "POST":
        source = unquote(parts[0])
        dm.restore_term(source, _term_id(query, body))
        reload_daemon()
        return _ok({"source": source, "terms": _terms_of(dm.list_terms(source))})
    if len(parts) == 2 and parts[1] == "terms":
        source = unquote(parts[0])
        if method == "GET":
            return _ok({"source": source, "terms": _terms_of(dm.list_terms(source))})
        if method == "POST":
            term, aka, note = _term_args(body)
            dm.add_term(source, term, aka, note)
        elif method == "PUT":
            term, aka, note = _term_args(body)
            dm.update_term(source, _term_id(query, body), term=term, aka=aka, note=note)
        elif method == "DELETE":
            dm.delete_term(source, _term_id(query, body))
        else:
            return None
        reload_daemon()
        return _ok({"source": source, "terms": _terms_of(dm.list_terms(source))})
    return None


def _test_view(result, text):
    """The UI view of dictmgmt.PhraseTest: before/after, applied aka -> term, prompt and hotwords, phonetic flags."""
    r = result if isinstance(result, dict) else {}
    applied = []
    for a in _first(r, "applied", default=[]) or []:
        if isinstance(a, dict):
            applied.append({"from": str(_first(a, "aka", "from", default="")),
                            "to": str(_first(a, "term", "to", default=""))})
        elif isinstance(a, (list, tuple)) and len(a) >= 2:
            applied.append({"from": str(a[0]), "to": str(a[1])})
    flags = [{"phrase": str(f.get("phrase", "")), "term": str(f.get("term", "")), "score": f.get("score")}
             for f in (_first(r, "flags", default=[]) or []) if isinstance(f, dict)]
    return {"before": str(_first(r, "before", default=text)), "after": str(_first(r, "after", default=text)),
            "applied": applied, "prompt": str(_first(r, "prompt", default="")),
            "hotwords": [str(h) for h in (_first(r, "hotwords", default=[]) or [])], "flags": flags,
            "warnings": [str(w) for w in (_first(r, "warnings", default=[]) or [])],
            "dropped": int(_first(r, "dropped", default=0) or 0)}


def route(method, path, query, body):
    """(status, content type, text) for a /api/dicts[/...] request, or None when `path` is not ours."""
    if path != "/api/dicts" and not path.startswith("/api/dicts/"):
        return None
    parts = [p for p in path[len("/api/dicts"):].split("/") if p]
    try:
        dm, stt_error = _stt()
    except Unavailable as e:
        return _unavailable(e)
    try:
        if not parts and method == "GET":
            return _ok(list_all(dm))
        out = _handle(dm, method, parts, query, body or {})
        return out if out is not None else _json(404, {"ok": False, "err": "404"})
    except BadRequest as e:
        return _json(400, {"ok": False, "err": str(e)})
    except stt_error as e:
        return _fail(e, 200 if method != "GET" else _get_status(e))


def _get_status(e):
    """A failed read must not look like data to the UI: 404 for an unknown dictionary, 422 otherwise."""
    return 404 if type(e).__name__ == "UnknownItemError" else 422

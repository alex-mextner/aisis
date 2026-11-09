"""Music Assistant (MA) voice/UI control + AirPlay receiver on the speaker.

One engine for two callers: the adapter (`POST /music/play`, voice «включи …»)
and the dashboard (`/api/music*`, `/api/airplay`). stdlib only.

Music path:   voice/UI -> MA REST `/api` (search, rank, `player_queues/play_media`)
              -> MA AirPlay provider -> station-airplay on the speaker (port 7000).
AirPlay path: phone -> station-airplay (skrashevich, YAM package) on the speaker.
              The receiver is configured through a small wrapper that reads
              /data/yam/var/aisis-airplay.conf (name, initial volume, output delay).

Config: env or ~/.config/aisis/ma.env (MA_URL, MA_TOKEN, MA_PLAYER_ID).
"""
from __future__ import annotations

import concurrent.futures
import difflib
import json
import logging
import os
import re
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

LOG = logging.getLogger(__name__)
MA_ENV_FILE = os.environ.get("AISIS_MA_ENV", os.path.expanduser("~/.config/aisis/ma.env"))
LAST_FILE = os.environ.get(
    "AISIS_MUSIC_LAST", os.path.expanduser("~/.local/state/aisis/music_last.json"))
ADB = os.environ.get("AISIS_ADB", "192.168.0.148:5555")
SPEAKER_IP = ADB.split(":")[0]

AP_BIN_DIR = "/data/yam/bin"
AP_STATE_DIR = "/data/yam/var/skrashevich.station-airplay"
AP_CONF = "/data/yam/var/aisis-airplay.conf"
AP_WRAPPER_MARK = "# aisis-conf"
# Spliced into the YAM package launcher just before its final `exec`; the launcher
# is the only place both the boot supervisor and manual starts pass through, and
# `yam doctor` stays green (it checks the public symlink, not the file contents).
AP_LAUNCHER_BLOCK = f"""{AP_WRAPPER_MARK}: apply {AP_CONF} (name / initial volume / output delay)
NAME= VOLUME_DB= DELAY_MS=
[ -r {AP_CONF} ] && . {AP_CONF}
[ -n "$DELAY_MS" ] && set -- -output-delay "${{DELAY_MS}}ms" "$@"
[ -n "$VOLUME_DB" ] && set -- -volume "$VOLUME_DB" "$@"
[ -n "$NAME" ] && set -- -name "$NAME" "$@"
"""
AP_LAUNCHER = f"$(readlink -f {AP_BIN_DIR}/station-airplay)"

AP_DEFAULT_VOLUME_DB = -20.0  # station-airplay flag defaults
AP_DEFAULT_DELAY_MS = 160
MEDIA_TYPES = ("artist", "album", "track", "playlist", "radio")
MATCH_THRESHOLD = 0.62
BARE_NAME_MIN_SCORE = 0.85  # a verb-less utterance («6 pistols») must match an artist this closely


# ── config / MA transport ───────────────────────────────────────────────────

def _read_env_file(path: str) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, _, value = line.partition("=")
                    out[key.strip()] = value.strip().strip("'\"")
    except OSError:
        pass
    return out


def _cfg() -> dict[str, str]:
    file_cfg = _read_env_file(MA_ENV_FILE)
    return {
        "url": (os.environ.get("MA_URL") or file_cfg.get("MA_URL") or "http://192.168.0.39:8095").rstrip("/"),
        "token": os.environ.get("MA_TOKEN") or file_cfg.get("MA_TOKEN") or "",
        "player": os.environ.get("MA_PLAYER_ID") or file_cfg.get("MA_PLAYER_ID") or "",
    }


class MaError(Exception):
    pass


def ma_call(command: str, args: dict[str, Any] | None = None, timeout: float = 20) -> Any:
    cfg = _cfg()
    if not cfg["token"]:
        raise MaError("MA_TOKEN не задан")
    req = urllib.request.Request(
        cfg["url"] + "/api",
        data=json.dumps({"command": command, "args": args or {}}).encode(),
        headers={"Authorization": "Bearer " + cfg["token"], "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode()
    except urllib.error.HTTPError as err:
        detail = err.read().decode(errors="replace")[:200]
        raise MaError(f"{command}: HTTP {err.code} {detail}") from err
    except (urllib.error.URLError, TimeoutError, OSError) as err:
        raise MaError(f"{command}: {err}") from err
    return json.loads(body) if body else None


def ma_info() -> dict[str, Any] | None:
    try:
        with urllib.request.urlopen(_cfg()["url"] + "/info", timeout=4) as resp:
            return json.loads(resp.read().decode())
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return None


def _player_id() -> str:
    """Target player: MA_PLAYER_ID, else the MA player whose device IP is the speaker's."""
    pinned = _cfg()["player"]
    if pinned:
        return pinned
    for player in ma_call("players/all") or []:
        ident = (player.get("device_info") or {}).get("ip_address")
        if ident == SPEAKER_IP and player.get("provider") == "universal_player":
            return player["player_id"]
    raise MaError(f"в MA нет плеера колонки ({SPEAKER_IP}); AirPlay-приёмник не найден")


# ── matching ────────────────────────────────────────────────────────────────

_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh", "з": "z",
    "и": "i", "й": "i", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r",
    "с": "s", "т": "t", "у": "u", "ф": "f", "х": "h", "ц": "ts", "ч": "ch", "ш": "sh",
    "щ": "sh", "ъ": "", "ы": "i", "ь": "", "э": "e", "ю": "u", "я": "ya",
}
_PHONETIC = (("x", "ks"), ("c", "k"), ("q", "k"), ("w", "v"), ("y", "i"), ("j", "i"), ("ph", "f"))


_DIGIT_WORDS = {"0": "zero", "1": "one", "2": "two", "3": "three", "4": "four",
                "5": "five", "6": "six", "7": "seven", "8": "eight", "9": "nine"}
def fold(text: str) -> str:
    """Case/punctuation/script-insensitive form so «секс пистолс» ~ «Sex Pistols».

    Digits fold to their English names on both sides: the recognizer writes «Sex Pistols» as
    «6 pistols», and «six»/«sex» fold to nearly the same letters.
    """
    s = text.lower().replace("ё", "е")
    s = re.sub(r"\d", lambda mo: f" {_DIGIT_WORDS[mo.group()]} ", s)
    s = "".join(_TRANSLIT.get(ch, ch) for ch in s)
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    for src, dst in _PHONETIC:
        s = s.replace(src, dst)
    s = re.sub(r"(.)\1+", r"\1", s)  # collapse doubles
    return re.sub(r"\s+", " ", s).strip()


def similarity(query: str, name: str) -> float:
    a, b = fold(query), fold(name)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    ratio = difflib.SequenceMatcher(None, a, b).ratio()
    a_tokens, b_tokens = set(a.split()), set(b.split())
    contained = a_tokens <= b_tokens or b_tokens <= a_tokens
    overlap = len(a_tokens & b_tokens) / max(len(a_tokens | b_tokens), 1)
    return max(ratio, 0.9 * overlap, 0.82 if contained and min(len(a_tokens), len(b_tokens)) >= 1 and ratio > 0.5 else 0.0)


def _item_artist(item: dict[str, Any]) -> str:
    artists = item.get("artists") or []
    if artists:
        return artists[0].get("name", "")
    return item.get("artist_str") or ""


def rank_results(query: str, results: dict[str, list[dict[str, Any]]],
                 media_type: str | None = None) -> list[dict[str, Any]]:
    """Return candidates best-first; each: {score, media_type, name, uri, artist}."""
    plural = {"artist": "artists", "album": "albums", "track": "tracks",
              "playlist": "playlists", "radio": "radio"}
    # «включи X» mostly names an artist; a track whose title merely sounds like the recognizer's text
    # («6 pistols» ~ track «Six Pistol») must not beat the band within an ASR-error margin.
    prior = {"artist": 0.08, "track": 0.0, "album": -0.01, "playlist": -0.02, "radio": -0.04}
    out: list[dict[str, Any]] = []
    for mtype, key in plural.items():
        if media_type and mtype != media_type:
            continue
        for item in results.get(key) or []:
            name = item.get("name") or ""
            artist = _item_artist(item)
            score = similarity(query, name)
            if artist:
                score = max(score, similarity(query, f"{name} {artist}"), similarity(query, f"{artist} {name}"))
            if "yandex_music" in (item.get("provider") or ""):
                score += 0.005  # prefer the Russian catalog on ties
            out.append({"score": round(min(1.0, score + prior[mtype]), 4), "media_type": mtype, "name": name,
                        "uri": item.get("uri"), "artist": artist})
    out.sort(key=lambda c: c["score"], reverse=True)
    return out


# ── last play record (shared between adapter and dashboard processes) ───────

def _save_last(record: dict[str, Any]) -> None:
    try:
        os.makedirs(os.path.dirname(LAST_FILE), exist_ok=True)
        with open(LAST_FILE, "w", encoding="utf-8") as fh:
            json.dump(record, fh, ensure_ascii=False)
    except OSError:
        pass


def _load_last() -> dict[str, Any] | None:
    try:
        with open(LAST_FILE, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


# ── music API ───────────────────────────────────────────────────────────────

def _current_media(player: dict[str, Any]) -> dict[str, Any] | None:
    media = player.get("current_media")
    if not media:
        return None
    return {
        "title": media.get("title") or media.get("uri"),
        "artist": media.get("artist"),
        "album": media.get("album"),
        "image": media.get("image_url"),
        "uri": media.get("uri"),
        "duration": media.get("duration"),
        "elapsed": player.get("elapsed_time"),
    }


def _player_brief(player: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": player["player_id"],
        "name": player.get("display_name") or player.get("name"),
        "state": player.get("playback_state"),
        "volume": player.get("volume_level"),
        "muted": player.get("volume_muted"),
        "available": player.get("available"),
        "current": _current_media(player),
    }


def music_state() -> dict[str, Any]:
    info = ma_info()
    state: dict[str, Any] = {
        "ma": {"reachable": info is not None, "version": (info or {}).get("server_version"),
               "url": _cfg()["url"], "player": None},
        "last": _load_last(),
        "targets": [],
    }
    if info is None:
        return state
    try:
        players = ma_call("players/all") or []
        state["targets"] = [
            {"id": p["player_id"], "name": p.get("display_name"), "state": p.get("playback_state")}
            for p in players
            if p.get("available") and p.get("enabled") and p.get("provider") == "universal_player"
        ]
        pid = _player_id()
        player = next((p for p in players if p["player_id"] == pid), None)
        if player:
            brief = _player_brief(player)
            try:
                queue = ma_call("player_queues/get", {"queue_id": pid}) or {}
                brief["queue_len"] = queue.get("items")
            except MaError:
                brief["queue_len"] = None
            state["ma"]["player"] = brief
    except MaError as err:
        state["ma"]["error"] = str(err)
    return state


# Spoken numerals the recognizer writes instead of a similar-sounding word («Sex Pistols» → «6 pistols»).
_DIGIT_HOMOPHONES = {"0": ["zero"], "1": ["one", "won"], "2": ["two", "to"], "4": ["four", "for"], "6": ["six", "sex"],
                     "8": ["eight"]}


def _query_variants(query: str) -> list[str]:
    """The query plus copies with each lone digit read as an English word or its homophone."""
    tokens = query.split()
    if not any(t in _DIGIT_HOMOPHONES for t in tokens):
        return [query]
    variants = [query]
    for index in range(max(len(words) for words in _DIGIT_HOMOPHONES.values())):
        swapped = " ".join(_DIGIT_HOMOPHONES[t][min(index, len(_DIGIT_HOMOPHONES[t]) - 1)]
                           if t in _DIGIT_HOMOPHONES else t for t in tokens)
        if swapped not in variants:
            variants.append(swapped)
    return variants


def music_resolve(query: str, media_type: str | None = None, min_score: float = MATCH_THRESHOLD) -> dict[str, Any]:
    """Search MA and pick the best match: {ok, matched, candidates} (no playback)."""
    query = query.strip()
    if not query:
        return {"ok": False, "err": "пустой запрос"}
    if media_type and media_type not in MEDIA_TYPES:
        return {"ok": False, "err": f"media_type: {media_type}"}

    def search(text: str) -> dict[str, list[dict[str, Any]]]:
        args: dict[str, Any] = {"search_query": text, "limit": 6}
        if media_type:
            args["media_types"] = [media_type]
        return ma_call("music/search", args, timeout=30) or {}

    variants = _query_variants(query)
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(variants)) as pool:
            found = list(pool.map(search, variants))
    except MaError as err:
        return {"ok": False, "err": str(err)}
    results: dict[str, list[dict[str, Any]]] = {}
    for one in found:
        for key, items in one.items():
            results.setdefault(key, []).extend(items)
    ranked = rank_results(query, results, media_type)
    if not ranked or ranked[0]["score"] < min_score:
        return {"ok": False, "err": "no_match", "candidates": ranked[:3]}
    return {"ok": True, "matched": ranked[0], "candidates": ranked[:3]}


CACHE_FILE = os.environ.get(
    "AISIS_MUSIC_CACHE", os.path.expanduser("~/.local/state/aisis/music_cache.json"))
CACHE_TTL_S = 30 * 86400
CACHE_MAX = 500
ARTIST_QUEUE_TRACKS = 25  # play the artist's first tracks, not MA's all_tracks fetch (+2 s, hundreds of items)
EARLY_ACK_FAIL_S = 0.25  # how long an early-ack play waits to surface an immediate MA error
_cache_lock = threading.Lock()
_play_pool = concurrent.futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix="music-play")


def _cache_key(query: str, media_type: str | None) -> str:
    return fold(query) + "|" + (media_type or "")


def _cache_load() -> dict[str, Any]:
    try:
        with open(CACHE_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _cache_write(data: dict[str, Any]) -> None:
    try:
        os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(CACHE_FILE), suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False)
        os.replace(tmp, CACHE_FILE)
    except OSError:
        pass


def _cache_get(key: str) -> dict[str, Any] | None:
    with _cache_lock:
        entry = _cache_load().get(key)
    if isinstance(entry, dict) and time.time() - entry.get("ts", 0) < CACHE_TTL_S and entry.get("matched"):
        return entry
    return None


def _cache_put(key: str, matched: dict[str, Any], media: Any) -> None:
    with _cache_lock:
        data = _cache_load()
        data[key] = {"ts": time.time(), "matched": matched, "media": media}
        if len(data) > CACHE_MAX:
            for stale in sorted(data, key=lambda k: data[k].get("ts", 0))[:len(data) - CACHE_MAX]:
                del data[stale]
        _cache_write(data)


def _cache_drop(key: str) -> None:
    with _cache_lock:
        data = _cache_load()
        if data.pop(key, None) is not None:
            _cache_write(data)


def _artist_media(matched: dict[str, Any]) -> Any:
    """URIs to queue for a matched item: an artist expands to its first tracks, the rest play as-is."""
    uri = matched["uri"]
    if matched["media_type"] == "artist" and "://artist/" in uri:
        provider, _, item_id = uri.partition("://artist/")
        try:
            tracks = ma_call("music/artists/artist_tracks",
                             {"item_id": item_id, "provider_instance_id_or_domain": provider}, timeout=15) or []
            uris = [t["uri"] for t in tracks[:ARTIST_QUEUE_TRACKS] if t.get("uri")]
            if uris:
                return uris
        except (MaError, KeyError, TypeError):
            pass
    return uri


def _play_media(pid: str, media: Any) -> None:
    ma_call("player_queues/play_media", {
        "queue_id": pid, "media": media, "option": "replace", "radio_mode": False,
    }, timeout=45)


def _finish_play(record: dict[str, Any], key: str, future: concurrent.futures.Future) -> None:
    err = future.exception()
    if err is None:
        return
    _cache_drop(key)
    record.update(ok=False, err=str(err))
    _save_last(record)
    LOG.warning("music_play_failed query=%r err=%s", record.get("query"), err)


def music_play(query: str, media_type: str | None = None, player_id: str | None = None,
               early_ack: bool = False, min_score: float = MATCH_THRESHOLD) -> dict[str, Any]:
    """Resolve `query` (cached after the first success) and start it on the speaker's AirPlay player.

    early_ack: return as soon as MA accepted the request (the AirPlay session takes ~4 s to
    produce sound), so a spoken confirmation overlaps the start instead of preceding it. An
    immediate MA error still surfaces; a later one is only logged and stored in the last-play record.
    """
    started = time.time()
    record: dict[str, Any] = {"query": query, "ts": int(started), "ok": False,
                              "matched": None, "err": None}
    key = _cache_key(query, media_type)
    cached = _cache_get(key)
    if cached is not None:
        matched, media = cached["matched"], cached.get("media") or cached["matched"]["uri"]
    else:
        resolved = music_resolve(query, media_type, min_score=min_score)
        if not resolved["ok"]:
            record["err"] = resolved["err"]
            _save_last(record)
            return {"ok": False, "err": resolved["err"], "candidates": resolved.get("candidates", [])}
        matched = resolved["matched"]
        media = _artist_media(matched)
        _cache_put(key, matched, media)
    try:
        pid = player_id or _player_id()
        if early_ack:
            record.update(ok=True, matched=matched)
            future = _play_pool.submit(_play_media, pid, media)
            future.add_done_callback(lambda f: _finish_play(record, key, f))
            try:
                future.result(timeout=EARLY_ACK_FAIL_S)
            except concurrent.futures.TimeoutError:
                pass  # still starting: MA is busy bringing up the AirPlay session
        else:
            _play_media(pid, media)
    except MaError as err:
        _cache_drop(key)
        record.update(ok=False, matched=matched, err=str(err))
        _save_last(record)
        return {"ok": False, "err": str(err), "matched": matched}
    record.update(ok=True, matched=matched)
    _save_last(record)
    return {"ok": True, "matched": matched, "ms": int((time.time() - started) * 1000),
            "cached": cached is not None}


_DASHBOARD_KEYS = {"pause": "pause", "resume": "resume", "stop": "stop", "next": "next", "prev": "previous"}
_CONTROL_ERRORS = {"idle": "Сейчас ничего не играет",
                   "unsupported_sender": "Сессию AirPlay держит телефон: ему можно только стоп и пауза"}


def music_control(action: str, value: Any = None) -> dict[str, Any]:
    """pause|resume|stop|next|prev|volume|mute -> {ok, state, err?}; state is re-read from MA.

    Transport keys go through media_control, so the dashboard silences the speaker exactly like the
    voice «стоп» does (including the receiver restart when MA does not deliver).
    """
    if action in _DASHBOARD_KEYS:
        result = media_control(_DASHBOARD_KEYS[action])
        if not result["ok"]:
            return {"ok": False, "err": _CONTROL_ERRORS.get(result["err"], result["err"])}
        time.sleep(0.6)
        return {"ok": True, "via": result["via"], "state": music_state()}
    try:
        pid = _player_id()
        if action == "volume":
            level = int(value)
            if not 0 <= level <= 100:
                return {"ok": False, "err": "volume 0..100"}
            ma_call("players/cmd/volume_set", {"player_id": pid, "volume_level": level})
        elif action == "mute":
            ma_call("players/cmd/volume_mute", {"player_id": pid, "muted": bool(value)})
        else:
            return {"ok": False, "err": f"неизвестное действие: {action}"}
    except (MaError, TypeError, ValueError) as err:
        return {"ok": False, "err": str(err)}
    time.sleep(0.6)
    return {"ok": True, "state": music_state()}


MEDIA_ACTIONS = ("stop", "pause", "resume", "next", "previous")
MA_CMD_TIMEOUT_S = 5       # a hung MA must not hold a voice «стоп»
MA_RESUME_TIMEOUT_S = 30   # resume waits for the whole AirPlay session to come back
STOP_SETTLE_S = 1.6        # how long the receiver may keep playing after MA acknowledged stop/pause
STOP_POLL_S = 0.3
KICK_SETTLE_S = 4.0         # a restarting receiver goes quiet within a second; the restart command itself takes ~3 s
MA_STATE_WAIT_S = 1.2      # MA may be too busy to say whether it plays; the receiver tells
PLAY_PENDING_S = 25        # a play request younger than this may still be starting
RESUME_WINDOW_S = 6 * 3600  # «продолжи» after a «стоп» replays the last thing we started within this window
RECEIVER_LOG = f"{AP_STATE_DIR}/receiver.log"
# Own pool: a media key must never queue behind a 45 s play_media on the two play workers.
_control_pool = concurrent.futures.ThreadPoolExecutor(max_workers=6, thread_name_prefix="media-key")


def _receiver_session() -> dict[str, Any] | None:
    """{connected, playing, peer} of the receiver's current AirPlay session (None: adb is down)."""
    rc, out = _adb(
        f"tail -c 200000 {RECEIVER_LOG} | grep 'alice_airplay_state' | tail -n 1; "
        f"echo '#'; tail -c 200000 {RECEIVER_LOG} | grep -o 'RTSP response peer=\"[0-9.]*\"' | tail -n 1", timeout=8)
    if rc != 0:
        return None
    state, _, peer = out.partition("#")
    ip = re.search(r'peer="([0-9.]+)"', peer)
    return {"connected": "connected=true" in state, "playing": "playing=true" in state,
            "peer": ip.group(1) if ip else None}


def _receiver_kick() -> bool:
    """End every AirPlay session by restarting the receiver; its output stops within a second.

    The only stop the standalone receiver offers from outside: a phone sender gets a dropped route
    (iOS pauses on it), MA gets a closed session. The restart itself takes ~3 s, so it runs detached
    and the caller watches the receiver's state instead.
    """
    rc, _ = _adb(f"{AP_BIN_DIR}/station-airplay-service restart >/dev/null 2>&1 &", timeout=10)
    return rc == 0


def _ma_player_state(pid: str) -> str | None:
    try:
        return (ma_call("players/get", {"player_id": pid}, timeout=4) or {}).get("playback_state")
    except MaError:
        return None


def _ma_media_key(action: str, pid: str, ma_state: str | None) -> None:
    """One media key on the MA queue.

    pause is a stop on purpose: MA acknowledges `players/cmd/pause` for this AirPlay 2 receiver but the
    player stays «playing» and the speaker keeps going (measured: 12 s, no MA log line). A stop keeps
    the queue position, and `player_queues/resume` continues from it, so «пауза» + «продолжи» still
    round-trips.
    """
    if action in ("stop", "pause"):
        ma_call("players/cmd/stop", {"player_id": pid}, timeout=MA_CMD_TIMEOUT_S)
    elif action == "resume" and ma_state == "paused":
        ma_call("players/cmd/play", {"player_id": pid}, timeout=MA_CMD_TIMEOUT_S)
    elif action == "resume":
        # MA brings the AirPlay session up again (seconds): acknowledge once it accepted the request.
        future = _control_pool.submit(ma_call, "player_queues/resume", {"queue_id": pid}, MA_RESUME_TIMEOUT_S)
        try:
            future.result(timeout=EARLY_ACK_FAIL_S)
        except concurrent.futures.TimeoutError:
            future.add_done_callback(lambda f: f.exception() and LOG.warning("media_resume_failed err=%s", f.exception()))
    elif action == "next":
        ma_call("player_queues/next", {"queue_id": pid}, timeout=MA_CMD_TIMEOUT_S)
    else:
        ma_call("player_queues/previous", {"queue_id": pid}, timeout=MA_CMD_TIMEOUT_S)


def _wait_receiver(playing: bool, window: float | None = None) -> bool:
    """True once the receiver's `playing` flag equals `playing` (False: it did not within `window`)."""
    deadline = time.time() + (STOP_SETTLE_S if window is None else window)
    while True:
        session = _receiver_session()
        if session is None or session["playing"] == playing:
            return True
        if time.time() >= deadline:
            return False
        time.sleep(STOP_POLL_S)


def media_control(action: str) -> dict[str, Any]:
    """Voice media key for whatever the speaker's AirPlay receiver is playing right now.

    stop|pause|resume|next|previous -> {ok, via: "ma"|"airplay"|"receiver"} or {ok: False, err}.
    err "idle": nothing of ours is playing (the caller may try Home Assistant instead);
    err "unsupported_sender": a phone owns the AirPlay session and only stop/pause/resume can reach it.
    The receiver is the ground truth for sound: stop/pause that MA does not deliver within STOP_SETTLE_S, and
    any stop/pause of a phone session the receiver does not pause itself, end in a receiver restart rather
    than a silent «ok». MA is slow while it starts a stream (seconds, cold: tens), so its answers never gate
    a stop that is audible.
    """
    if action not in MEDIA_ACTIONS:
        return {"ok": False, "err": f"неизвестное действие: {action}"}
    try:
        pid = _player_id()
    except MaError as err:
        return {"ok": False, "err": str(err)}
    session_future = _control_pool.submit(_receiver_session)
    state_future = _control_pool.submit(_ma_player_state, pid)
    session = session_future.result()
    try:
        ma_state = state_future.result(timeout=MA_STATE_WAIT_S)
    except concurrent.futures.TimeoutError:
        ma_state = None
    connected = bool(session and session["connected"])
    playing = bool(session and session["playing"])
    # MA reports «idle» for ~5 s after the speaker is already audible, and a play request it has accepted
    # may still be starting: the AirPlay peer and the last play record decide too.
    ma_owns = (ma_state in ("playing", "paused") or _play_pending()
               or (connected and session["peer"] == urllib.parse.urlsplit(_cfg()["url"]).hostname))
    if action in ("stop", "pause"):
        return _silence(action, pid, ma_state, ma_owns, connected, playing, session is None)
    # resume / next / previous: only MA has a queue to drive.
    resumable = action == "resume" and ma_state == "idle" and _resume_window_open()
    if not ma_owns and not resumable:
        if connected and action == "resume" and _wait_receiver(True):
            return {"ok": True, "via": "airplay"}
        return {"ok": False, "err": "unsupported_sender" if connected else "idle"}
    try:
        _ma_media_key(action, pid, ma_state)
    except MaError as err:
        return {"ok": False, "err": str(err)}
    return {"ok": True, "via": "ma"}


def _silence(action: str, pid: str, ma_state: str | None, ma_owns: bool, connected: bool, playing: bool,
             adb_down: bool) -> dict[str, Any]:
    if not ma_owns and not connected:
        return {"ok": False, "err": "idle"}
    if action == "pause" and not ma_owns and not playing:
        return {"ok": False, "err": "idle"}
    command = _control_pool.submit(_ma_media_key, action, pid, ma_state) if ma_owns else None
    if command is not None and not playing and not adb_down:
        # Nothing audible yet (MA is still bringing the stream up): the command itself must land, or the
        # music starts after the «стоп».
        try:
            command.result(timeout=MA_CMD_TIMEOUT_S)
        except (MaError, concurrent.futures.TimeoutError) as err:
            LOG.warning("media_key_failed action=%s err=%s", action, err)
            return {"ok": False, "err": f"MA не ответил: {err}"}
        return {"ok": True, "via": "ma"}
    if adb_down:
        if command is None:
            return {"ok": False, "err": "adb недоступен"}
        try:
            command.result(timeout=MA_CMD_TIMEOUT_S)
        except (MaError, concurrent.futures.TimeoutError) as err:
            return {"ok": False, "err": str(err)}
        return {"ok": True, "via": "ma"}
    # Audible: MA's command races the receiver. A phone owns the session when MA does not: the voice turn also
    # told the receiver the stock «пауза» sentence, which it turns into the phone's own pause.
    if _wait_receiver(False):
        delivered = command is not None and command.done() and command.exception() is None
        return {"ok": True, "via": "ma" if delivered else "airplay"}
    if command is not None:
        command.add_done_callback(lambda f: f.exception() and LOG.warning("media_key_failed action=%s err=%s", action, f.exception()))
    if _receiver_kick() and _wait_receiver(False, KICK_SETTLE_S):
        return {"ok": True, "via": "receiver"}
    return {"ok": False, "err": "не удалось остановить приёмник"}


def _play_pending() -> bool:
    """A play request was accepted moments ago and MA may still be starting the stream."""
    last = _load_last()
    return bool(last and last.get("ok") and time.time() - last.get("ts", 0) < PLAY_PENDING_S)


def _resume_window_open() -> bool:
    last = _load_last()
    return bool(last and last.get("ok") and time.time() - last.get("ts", 0) < RESUME_WINDOW_S)


# ── AirPlay receiver on the speaker (adb) ───────────────────────────────────

def _adb(command: str, timeout: float = 20) -> tuple[int, str]:
    try:
        proc = subprocess.run(["adb", "-s", ADB, "shell", command],
                              capture_output=True, text=True, timeout=timeout)
        return proc.returncode, (proc.stdout + proc.stderr).strip()
    except (OSError, subprocess.TimeoutExpired) as err:
        return 1, str(err)[:200]


def _adb_push(content: str, remote: str, mode: str = "0644") -> bool:
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False, suffix=".tmp") as fh:
        fh.write(content)
        local = fh.name
    try:
        proc = subprocess.run(["adb", "-s", ADB, "push", local, remote],
                              capture_output=True, text=True, timeout=20)
        if proc.returncode != 0:
            return False
        rc, _ = _adb(f"chmod {mode} {remote}")
        return rc == 0
    except (OSError, subprocess.TimeoutExpired):
        return False
    finally:
        os.unlink(local)


_AP_PROBE = f"""
export PATH=$PATH:{AP_BIN_DIR}
echo "installed=$([ -x {AP_BIN_DIR}/station-airplay ] && echo 1 || echo 0)"
echo "running=$(station-airplay-service status >/dev/null 2>&1 && echo 1 || echo 0)"
echo "disabled=$([ -f {AP_STATE_DIR}/disabled ] && echo 1 || echo 0)"
echo "wrapper=$(grep -c '{AP_WRAPPER_MARK}' {AP_LAUNCHER} 2>/dev/null)"
echo "version=$(station-airplay -version 2>&1 | head -1)"
echo "cmdline=$(ps | grep 'share/skrashevich.station-airplay/bin/station-airplay' | grep -v grep | head -1)"
echo "conf_begin"; cat {AP_CONF} 2>/dev/null; echo "conf_end"
echo "bt_begin"; bluetoothctl show 2>/dev/null | grep -E 'Name:|Alias:|Powered:|Discoverable:|Pairable:'; echo "bt_end"
echo "btdev_begin"; bluetoothctl devices Paired 2>/dev/null || bluetoothctl paired-devices 2>/dev/null; echo "btdev_end"
echo "log_begin"; grep -h ' ready name=' {AP_STATE_DIR}/receiver.log 2>/dev/null | tail -n 1
grep -h 'alice_airplay_state' {AP_STATE_DIR}/receiver.log 2>/dev/null | tail -n 1
tail -n 8 {AP_STATE_DIR}/receiver.log 2>/dev/null; echo "log_end"
"""


def _section(text: str, name: str) -> list[str]:
    match = re.search(rf"^{name}_begin\n(.*?)^{name}_end$", text, re.S | re.M)
    return [ln for ln in match.group(1).splitlines() if ln.strip()] if match else []


def _parse_conf(lines: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for ln in lines:
        m = re.match(r"^(NAME|VOLUME_DB|DELAY_MS)='(.*)'$", ln)
        if m:
            out[m.group(1)] = m.group(2).replace("'\\''", "'")
    return out


def airplay_state() -> dict[str, Any]:
    rc, out = _adb(_AP_PROBE, timeout=25)
    base: dict[str, Any] = {"installed": False, "running": False, "adb": False}
    if rc != 0 or "installed=" not in out:
        base["err"] = out[:200] or "adb недоступен"
        return base
    kv = dict(re.findall(r"^(installed|running|disabled|wrapper|version|cmdline)=(.*)$", out, re.M))
    log = _section(out, "log")
    conf = _parse_conf(_section(out, "conf"))
    cmdline = kv.get("cmdline", "")

    def flag(name: str, cast: Any = str) -> Any:
        m = re.search(rf"(?:^|\s)-{name}[ =](\S+)", cmdline)
        return cast(m.group(1)) if m else None

    ready = [ln for ln in log if " ready name=" in ln]
    ready_name = re.search(r'ready name="([^"]*)"', ready[-1]).group(1) if ready else None
    air = [ln for ln in log if "alice_airplay_state" in ln]
    connected = playing = False
    if air:
        connected = "connected=true" in air[-1]
        playing = "playing=true" in air[-1]
    bt = {k.strip().lower(): v.strip() for k, _, v in (ln.partition(":") for ln in _section(out, "bt"))}
    bt_devs = [ln.split(" ", 2)[2] for ln in _section(out, "btdev") if ln.startswith("Device ")]
    delay = flag("output-delay")
    running = kv.get("running") == "1"
    return {
        "adb": True,
        "installed": kv.get("installed") == "1",
        "running": running,
        "enabled": kv.get("disabled") == "0",
        "version": kv.get("version") or None,
        "name": ready_name,
        "name_configured": conf.get("NAME") or None,
        "volume_db": float(flag("volume") or conf.get("VOLUME_DB") or AP_DEFAULT_VOLUME_DB),
        "output_delay_ms": int(re.sub(r"\D", "", delay)) if delay else int(conf.get("DELAY_MS") or AP_DEFAULT_DELAY_MS),
        "wrapper": kv.get("wrapper") == "1",
        "listening": _tcp_open(SPEAKER_IP, 7000),
        "connected": connected,
        "playing": playing,
        "device_ip": SPEAKER_IP,
        "log_tail": [ln[-200:] for ln in log[-8:]],
        "bluetooth": {
            "name": bt.get("alias") or bt.get("name"),
            "powered": bt.get("powered") == "yes",
            "discoverable": bt.get("discoverable") == "yes",
            "pairable": bt.get("pairable") == "yes",
            "paired": bt_devs,
        } if bt else None,
    }


def _tcp_open(host: str, port: int) -> bool:
    import socket
    try:
        with socket.create_connection((host, port), timeout=2):
            return True
    except OSError:
        return False


def _sh_quote(value: str) -> str:
    return "'" + value.replace("'", "'\\''") + "'"


def _validate(changes: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
    clean: dict[str, Any] = {}
    if "name" in changes and changes["name"] is None:
        clean["NAME"] = ""  # reset to the receiver default (hostname + AirPlay)
    elif "name" in changes:
        name = str(changes["name"]).strip()
        if not name or len(name.encode()) > 60 or re.search(r"[\x00-\x1f\x7f]", name):
            return {}, "name: 1..60 байт, без управляющих символов"
        clean["NAME"] = name
    if "volume_db" in changes:
        try:
            vol = float(changes["volume_db"])
        except (TypeError, ValueError):
            return {}, "volume_db: число"
        if not -30.0 <= vol <= 0.0:
            return {}, "volume_db: -30..0"
        clean["VOLUME_DB"] = f"{vol:g}"
    if "output_delay_ms" in changes:
        try:
            delay = int(changes["output_delay_ms"])
        except (TypeError, ValueError):
            return {}, "output_delay_ms: целое"
        if not 0 <= delay <= 2000:
            return {}, "output_delay_ms: 0..2000"
        clean["DELAY_MS"] = str(delay)
    return clean, None


def _wait_ready(since_lines_marker: str | None, timeout: float = 25) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        rc, out = _adb(f"tail -n 30 {AP_STATE_DIR}/receiver.log")
        ready = [ln for ln in out.splitlines() if " ready name=" in ln]
        if ready and ready[-1] != since_lines_marker:
            return True
        time.sleep(1.2)
    return False


def airplay_set(changes: dict[str, Any]) -> dict[str, Any]:
    """Apply name/volume_db/output_delay_ms/enabled/restart/bluetooth_pairing and read back.

    Returns {ok, state, applied:{key: bool}, err?}; `applied` compares the running
    process (cmdline / log / bluetoothctl) with what was requested.
    """
    before = airplay_state()
    if not before.get("adb"):
        return {"ok": False, "err": before.get("err", "adb недоступен"), "state": before}
    if not before["installed"]:
        return {"ok": False, "err": "station-airplay не установлен", "state": before}
    conf_changes, err = _validate(changes)
    if err:
        return {"ok": False, "err": err, "state": before}

    if "bluetooth_pairing" in changes:
        on = bool(changes["bluetooth_pairing"])
        secs = int(changes.get("bluetooth_seconds") or 120)
        cmds = (f"bluetoothctl discoverable-timeout {secs}; bluetoothctl pairable yes; bluetoothctl discoverable yes"
                if on else "bluetoothctl discoverable no; bluetoothctl pairable no")
        _adb(cmds)

    wants_restart = bool(changes.get("restart")) or bool(conf_changes)
    if conf_changes:
        merged = {"NAME": before.get("name_configured") or "",
                  "VOLUME_DB": f"{before['volume_db']:g}",
                  "DELAY_MS": str(before["output_delay_ms"])}
        merged.update(conf_changes)
        body = "".join(f"{k}={_sh_quote(v)}\n" for k, v in merged.items() if v != "")
        if not _adb_push(body, AP_CONF, "0644"):
            return {"ok": False, "err": "не удалось записать конфиг на колонку", "state": before}
    if not before["wrapper"] and (conf_changes or changes.get("restart")):
        if not _install_wrapper():
            return {"ok": False, "err": "не удалось поставить wrapper", "state": before}
        wants_restart = True

    marker = None
    rc, log = _adb(f"tail -n 30 {AP_STATE_DIR}/receiver.log")
    ready = [ln for ln in log.splitlines() if " ready name=" in ln]
    if ready:
        marker = ready[-1]

    enabled = changes.get("enabled")
    if enabled is False:
        _adb(f"{AP_BIN_DIR}/station-airplay-service disable", timeout=30)
    elif enabled is True and not before["enabled"]:
        _adb(f"{AP_BIN_DIR}/station-airplay-service enable", timeout=30)
        wants_restart = False
    elif wants_restart and (before["running"] or enabled is True):
        _adb(f"{AP_BIN_DIR}/station-airplay-service restart", timeout=40)
    elif wants_restart and not before["running"] and before["enabled"]:
        _adb(f"{AP_BIN_DIR}/station-airplay-service start", timeout=30)

    if (wants_restart or enabled) and enabled is not False:
        _wait_ready(marker)
    state = airplay_state()
    applied: dict[str, bool] = {}
    if "NAME" in conf_changes:
        applied["name"] = (state.get("name") == conf_changes["NAME"] if conf_changes["NAME"]
                           else state.get("name_configured") is None and bool(state.get("name")))
    if "VOLUME_DB" in conf_changes:
        applied["volume_db"] = state.get("volume_db") == float(conf_changes["VOLUME_DB"])
    if "DELAY_MS" in conf_changes:
        applied["output_delay_ms"] = state.get("output_delay_ms") == int(conf_changes["DELAY_MS"])
    if enabled is not None:
        applied["enabled"] = bool(state.get("enabled")) == bool(enabled) and bool(state.get("running")) == bool(enabled)
    if "bluetooth_pairing" in changes and state.get("bluetooth"):
        applied["bluetooth_pairing"] = bool(state["bluetooth"]["discoverable"]) == bool(changes["bluetooth_pairing"])
    return {"ok": all(applied.values()), "applied": applied, "state": state}


def _install_wrapper() -> bool:
    """Splice the conf-reading block into the package launcher (idempotent)."""
    rc, path = _adb(f"readlink -f {AP_BIN_DIR}/station-airplay")
    if rc != 0 or not path.startswith("/"):
        return False
    rc, original = _adb(f"cat {path}")
    if rc != 0 or "exec " not in original:
        return False
    if AP_WRAPPER_MARK in original:
        return True
    head, sep, tail = original.rpartition("\nexec ")
    if not sep:
        return False
    patched = head + "\n" + AP_LAUNCHER_BLOCK + "exec " + tail + "\n"
    staged = path + ".aisis-new"
    if not _adb_push(patched, staged, "0755"):
        return False
    rc, _ = _adb(f"mv -f {staged} {path}")
    return rc == 0

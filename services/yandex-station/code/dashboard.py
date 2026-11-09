#!/usr/bin/env python3
"""Control center: SPA on :8781. /events.json + /api/* backend, no secrets in frontend."""
import re, subprocess, html, json, time, os, urllib.request, threading, http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote
import dialogstore
import voiceclips
import clipfeed
import cliptruth
import stt_tuning
import dsp_settings
import sys
import speaker_settings
import tts_settings
import led_animation
import user_prompt
import tvpause
import yse
import dashboard_dicts

VERSION = "2.0.0"
STARTED = time.time()
PORT = int(os.environ.get("AISIS_DASHBOARD_PORT") or 8781)
QUASAR_API = "http://127.0.0.1:31116"
DUID = "M00ZRW3008RAJG"
ADB = "192.168.0.148:5555"
HA_URL = os.environ.get("HA_URL", "http://192.168.0.39:8123")
HA_TOKEN = os.environ.get("HA_TOKEN", "")
HA_ENTITY = "media_player.aisis_speaker_m00zrw3008rajg"
CORPUS_DIR = os.path.expanduser("~/xp/aisis/.worktrees/claude-primary-api/backend/yandex/corpus")
BLUEGREEN_STATE = os.environ.get("AISIS_BLUEGREEN_STATE") or os.path.expanduser(
    "~/.local/share/aisis/bluegreen-state/state.env")
BLUEGREEN_COLORS = ("blue", "green")
SELF_UNIT = "aisis-dashboard.service"

# name -> (label, role). Restart allow-list for docker = these keys.
DOCKER_SVCS = {
    "aisis-quasar-prod-blue": ("Quasar prod blue", "quasar-server"),
    "aisis-quasar-prod-green": ("Quasar prod green", "quasar-server"),
    "aisis-switch-prod": ("Switch prod", "HTTPS-переключатель blue/green (:31116)"),
    "aisis-yandex-network-tls-voice-1": ("TLS voice", "TLS-терминатор голосового трафика"),
    "aisis-yandex-network-dns-relay-1": ("DNS relay", "DNS-реле колонки"),
    "aisis-yandex-postgres-1": ("Postgres", "база quasar-server"),
    "dozzle": ("Dozzle", "просмотр логов контейнеров"),
    "aisis-openclaw-gateway": ("OpenClaw gateway", "шлюз OpenClaw"),
}
# systemd --user units; units not listed here but named aisis-*.service are shown with their own name.
SYSTEMD_META = {
    "aisis-station-adapter.service": ("Адаптер", "адаптер ChatGPT и Bracho"),
    "aisis-station-stt.service": ("STT", "потоковый шлюз распознавания речи"),
    "aisis-station-pm.service": ("PM", "речевые промпты через ChatGPT-браузер"),
    "aisis-station-embed.service": ("Embed", "эмбеддинги памяти (e5-small ONNX)"),
    "aisis-station-lan-proxy.service": ("Quasar LAN proxy", "LAN-прокси quasar-server (:31116)"),
    "aisis-dashboard.service": ("Дашборд", "этот интерфейс :8781"),
    "aisis-clipfeed.service": ("Лента клипов", "реплики с записями в Telegram, сбор эталона (:31124)"),
    "aisis-station-kit.service": ("Kit", "sync-сервер колонки (quasar.yandex.net)"),
    "aisis-ha-cli.service": ("HA CLI", "прокси Home Assistant с кэшем (:8795)"),
    "aisis-failover.service": ("Failover", "автофейловер голосового стека"),
}

def sh(cmd, timeout=15):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout + p.stderr).strip()
    except Exception as e:
        return 1, str(e)[:200]

def http_json(url, method="GET", payload=None, headers=None, timeout=8):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    if data:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode() or "null")

def ha(path, payload=None):
    return http_json(f"{HA_URL}{path}", "POST" if payload is not None else "GET", payload,
                     {"Authorization": f"Bearer {HA_TOKEN}"})

# ---------- dialogs (persistent SQLite, filled by dialogstore.Ingester) ----------
INGESTER = None

CLIP_FIELDS = ("id", "ts", "dur", "transcript", "status", "reason", "peak", "rms", "voiced_ms", "contrast",
               "asr_ms", "final_ms", "trigger", "engine")

def attach_clips(dialogs):
    """Archived utterances (voiceclips) onto each voice dialog as `clips`; never fails the page."""
    for d in dialogs:
        d["clips"] = []
        if d.get("kind") != "voice":
            continue
        try:
            d["clips"] = [{k: c[k] for k in CLIP_FIELDS} for c in voiceclips.clips_for_dialog(dialogstore.DB_PATH, d["id"])]
            cliptruth.annotate(dialogstore.DB_PATH, d["clips"])
        except Exception:
            pass

def voice_archive_state():
    try:
        return {"available": True, **voiceclips.usage(dialogstore.DB_PATH)}
    except Exception as e:
        return {"available": False, "err": str(e)[:160]}

def asr_bench_state():
    """Nightly ASR candidate benchmark over the voice archive (bench_asr_archive.py); never fails the page."""
    try:
        import bench_asr_archive
        return {"available": True, **bench_asr_archive.summary(dialogstore.DB_PATH)}
    except Exception as e:
        return {"available": False, "err": str(e)[:160]}

def clipfeed_state():
    """Telegram clip feed: settings + counters (read from SQLite, so it works while the service is down)."""
    try:
        return {"available": True, **clipfeed.feed_state(dialogstore.DB_PATH)}
    except Exception as e:
        return {"available": False, "err": str(e)[:160]}

def clipfeed_save(body):
    try:
        clipfeed.update_settings({k: body[k] for k in clipfeed.DEFAULTS if k in body and k != "tg_session"})
    except ValueError as e:
        return {"ok": False, "err": str(e)}
    return {"ok": True, **clipfeed_state()}

def clip_truth_state(qs):
    """Owner-verified truth: WER stats plus a page of clips (`mode` judged|pending|all, `limit`, `before`)."""
    try:
        mode = qs.get("mode", ["judged"])[0]
        limit = int(qs.get("limit", ["30"])[0])
        before = float(qs["before"][0]) if "before" in qs else None
        return {"available": True, "stats": cliptruth.stats(dialogstore.DB_PATH),
                **cliptruth.list_clips(dialogstore.DB_PATH, mode, limit, before)}
    except (ValueError, cliptruth.TruthError) as e:
        return {"available": False, "err": f"плохой запрос: {e}"}
    except Exception as e:
        return {"available": False, "err": str(e)[:160]}

def clip_truth_save(body):
    """The same verdict words as in Telegram: text, "=" (верно), "шум" (не речь)."""
    clip_id, text = body.get("clip_id"), body.get("text")
    if not isinstance(clip_id, str) or not isinstance(text, str):
        return {"ok": False, "err": "нужны clip_id и text"}
    try:
        label, truth = cliptruth.parse_reply(text)
        return {"ok": True, "truth": cliptruth.set_truth(dialogstore.DB_PATH, clip_id, label, truth, source="dashboard")}
    except cliptruth.TruthError as e:
        return {"ok": False, "err": str(e)}

def stt_tuning_save(body):
    """End-of-speech wait of the STT gateway; applies to the next conversation, no restart."""
    try:
        return {"ok": True, **stt_tuning.update(body.get("silence_ms"))}
    except ValueError as e:
        return {"ok": False, "err": str(e)}

def stt_dsp_save(body):
    """Level front-end of the recognizer copy (dsp_settings.py); applies to the next conversation, no restart.
    {"reset": true} drops every override back to the runtime.env defaults."""
    try:
        if body.get("reset") is True:
            dsp_settings.reset()
        else:
            dsp_settings.update({k: v for k, v in body.items() if k in dsp_settings.KEYS})
    except ValueError as e:
        return {"ok": False, "err": str(e)}
    return {"ok": True, **dsp_settings.state(dialogstore.DB_PATH)}


CLIP_TYPES = {".opus": "audio/ogg", ".wav": "audio/wav"}

def parse_range(header, size):
    """(start, end) inclusive for a single `bytes=` range, None when absent/unsatisfiable-as-full, False when invalid."""
    if not header:
        return None
    m = re.fullmatch(r"bytes=(\d*)-(\d*)", header.strip())
    if not m or (not m[1] and not m[2]):
        return None
    if m[1]:
        start = int(m[1])
        end = min(int(m[2]), size - 1) if m[2] else size - 1
    else:
        n = int(m[2])
        if n == 0:
            return False
        start, end = max(0, size - n), size - 1
    if start >= size or start > end:
        return False
    return start, end

def events_json(limit=50, before=None, q=None, since=None, until=None):
    try:
        conn = dialogstore.connect(dialogstore.DB_PATH)
        try:
            d = dialogstore.page(conn, limit, before, q=q, since=since, until=until)
            d.update(dialogstore.log_summary(conn))
        finally:
            conn.close()
        attach_clips(d["dialogs"])
        d["containerRunning"] = bool(INGESTER and INGESTER.status["running"])
        return d
    except Exception as e:
        return {"error": str(e)[:200]}

def dialogs_stats():
    conn = dialogstore.connect()
    try:
        s = dialogstore.stats(conn)
    finally:
        conn.close()
    if INGESTER:
        s["containers"] = INGESTER.status["containers"]
        s["dockerOk"] = INGESTER.status["dockerOk"]
    return s

# ---------- speaker ----------
_fw_cache = {"t": 0, "v": None}

def adb_firmware():
    if time.time() - _fw_cache["t"] < 300:
        return _fw_cache["v"]
    rc, out = sh(["adb", "-s", ADB, "shell", "cat /etc/os-release /etc/issue"], 8)
    m = re.search(r"VERSION=(\S+)", out)
    v = m.group(1) if m else None
    _fw_cache.update(t=time.time(), v=v)
    return v

DEVICE_CONFIG_KEYS = ("ContentSettings", "Spotter", "ChildContentSettings", "SmartActivation", "SmartVolumeEnabled")
DEFAULT_MAX_LEVEL = 10
_audio_lock = threading.Lock()   # serializes volume/mute: mute's remembered level must match what it muted from

def quasar_device():
    """This speaker's entry of GET /devices (None when the API doesn't list it)."""
    devs = http_json(f"{QUASAR_API}/devices")
    return next((d for d in devs if d["duid"] == DUID), None)

def quasar_state():
    return http_json(f"{QUASAR_API}/devices/{DUID}/state")

def speaker_state():
    info = {"duid": DUID}
    try:
        me = quasar_device()
        info["name"] = me["name"] if me else "?"
        info["connected"] = bool(me and me["connected"])
    except Exception as e:
        info["connected"], info["apiErr"] = False, str(e)[:120]
    ds = {}
    try:
        st = quasar_state()
        ds = st.get("deviceState", {})
        player = ds.get("AudioPlayer") or {}
        net = ds.get("InternetConnection") or {}
        info.update(soundLevel=ds.get("SoundLevel"), soundMaxLevel=ds.get("SoundMaxLevel"),
                    soundMuted=ds.get("SoundMuted"), micsMuted=ds.get("MicsMuted"),
                    player=player.get("PlayerState"),
                    playerTitle=(player.get("CurrentlyPlaying") or {}).get("Title") or None,
                    wifi=net.get("Type"), ssid=(net.get("Current") or {}).get("Ssid"),
                    model=(st.get("info") or {}).get("platform"))
    except Exception as e:
        info["stateErr"] = str(e)[:120]
    info["deviceConfig"] = {k: v for k, v in (ds.get("DeviceConfig") or {}).items() if k in DEVICE_CONFIG_KEYS}
    info["adbOnline"] = speaker_settings.adb_online(ADB)
    info["firmware"] = adb_firmware()
    info["ha"] = None
    if HA_TOKEN:
        try:
            s = http_json(f"{HA_URL}/api/states/{HA_ENTITY}",
                          headers={"Authorization": f"Bearer {HA_TOKEN}"})
            info["ha"] = {"state": s.get("state"), "volume": s.get("attributes", {}).get("volume_level")}
        except Exception as e:
            info["haErr"] = str(e)[:120]
    info["uiMute"] = speaker_settings.ui_mute(info.get("soundLevel"),
                                               speaker_settings.load_ui_state()["restoreLevel"])
    info["updated"] = time.strftime("%H:%M:%S")
    return info

def read_sound():
    """(level, max level) from the speaker's reported state."""
    ds = quasar_state().get("deviceState", {})
    return ds.get("SoundLevel"), ds.get("SoundMaxLevel") or DEFAULT_MAX_LEVEL

def push_volume(v, max_level):
    body = [{"name": "smart_speaker_set_sound_volume", "arguments": {"volume": int(v)}}]
    try:
        http_json(f"{QUASAR_API}/devices/{DUID}/push", "POST", body)
        return "quasar"
    except Exception as e:
        if not HA_TOKEN:
            raise
        try:
            ha("/api/services/media_player/volume_set",
               {"entity_id": HA_ENTITY, "volume_level": round(min(max(v, 0), max_level) / max_level, 2)})
            return "ha"
        except Exception as e2:
            raise RuntimeError(f"{str(e)[:80]} / {str(e2)[:80]}") from e2

def apply_volume(v, max_level, wait_s=6.0, interval_s=0.5):
    """Push the level, then poll the speaker's state until it reports it. Caller holds _audio_lock."""
    try:
        via = push_volume(v, max_level)
    except Exception as e:
        return {"ok": False, "requested": v, "err": str(e)[:160]}
    level, deadline = None, time.monotonic() + wait_s
    while True:
        try:
            level = read_sound()[0]
        except Exception:
            pass
        if level == v or time.monotonic() + interval_s > deadline:
            break
        time.sleep(interval_s)
    return {"ok": True, "requested": v, "level": level, "applied": level == v, "via": via}

def api_volume(body):
    try:
        _, max_level = read_sound()
    except Exception as e:
        return {"ok": False, "err": f"состояние колонки недоступно: {str(e)[:120]}"}
    try:
        v = speaker_settings.parse_volume(body.get("volume"), max_level)
    except ValueError as e:
        return {"ok": False, "err": str(e)}
    with _audio_lock:
        return apply_volume(v, max_level)

def api_mute(muted):
    """Mute = volume 0 with the audible level remembered (HA volume_mute is unsupported by the entity)."""
    if not isinstance(muted, bool):
        return {"ok": False, "err": "muted: нужно true/false"}
    with _audio_lock:
        try:
            level, max_level = read_sound()
        except Exception as e:
            return {"ok": False, "err": f"состояние колонки недоступно: {str(e)[:120]}"}
        ui = speaker_settings.load_ui_state()
        target, restore = speaker_settings.mute_plan(muted, level, ui["restoreLevel"], max_level)
        if restore != ui["restoreLevel"]:
            speaker_settings.save_ui_state({"restoreLevel": restore})
        res = apply_volume(target, max_level)
    if not res["ok"]:
        return res
    return {"ok": True, "muted": res["level"] == 0 if res["level"] is not None else muted,
            "level": res["level"], "restoreLevel": restore, "applied": res["applied"]}

# ---------- speaker settings (speaker_settings.py: overlay for the kit + readback) ----------
RESYNC_WAIT_S = 12.0

def patch_quasar(patch):
    """PATCH the quasar-server config (DB + re-sync ping to the speaker); returns its quasarConfig."""
    resp = http_json(f"{QUASAR_API}/devices/{DUID}", "PATCH", patch, timeout=15)
    return (resp or {}).get("quasarConfig")

def speaker_settings_state():
    out = {"schema": speaker_settings.schema(), "overlayActive": speaker_settings.overlay_active()}
    values = speaker_settings.flatten_quasar(None)
    try:
        me = quasar_device()
        if me:
            values = speaker_settings.flatten_quasar(me.get("quasarConfig"))
    except Exception as e:
        out["apiErr"] = str(e)[:120]
    live = {}
    try:
        live = quasar_state().get("deviceState", {}).get("DeviceConfig") or {}
    except Exception as e:
        out["stateErr"] = str(e)[:120]
    dev = speaker_settings.adb_read_device_config(ADB)
    out.update(values=values, device=dev.values, liveState=live, adbOnline=dev.online, deviceFileAgeS=dev.age_s)
    return out

def _verify_settings(desired, server_config, wait_s, extra=None):
    """Poll user_config.dat until the desired keys match (or wait_s), return the results envelope."""
    server_flat = speaker_settings.flatten_quasar(server_config)
    dev, waited = speaker_settings.wait_for_device(
        desired, lambda: speaker_settings.adb_read_device_config(ADB), max_wait_s=wait_s)
    return {**(extra or {}), "results": speaker_settings.build_results(desired, server_flat, dev),
            "waitedS": waited, "adbOnline": dev.online}

def speaker_settings_apply(body):
    try:
        changes = speaker_settings.validate_changes(body.get("changes"))
    except ValueError as e:
        return {"ok": False, "err": str(e)}
    try:
        speaker_settings.save_overlay_changes(changes)
    except OSError as e:
        return {"ok": False, "err": f"overlay не записан: {str(e)[:120]}"}
    try:
        server_config = patch_quasar(speaker_settings.quasar_patch(changes))
    except Exception as e:
        # Overlay is saved and reaches the speaker on its next periodic sync; no re-sync ping happened,
        # so one readback is all that can be said now.
        res = _verify_settings(changes, None, 0, {"ok": False, "overlaySaved": True,
                                                  "err": f"quasar-server: {str(e)[:120]}"})
        return res
    return _verify_settings(changes, server_config, speaker_settings.SETTINGS_WAIT_S, {"ok": True})

def speaker_settings_resync():
    """Make the speaker converge on the dashboard's values: keys the device holds with a different value
    (e.g. the kit template's jingle=true vs the DB's false) are written to the overlay, then the speaker is
    pinged to re-sync; every key is reported (desired = overlay value, else quasar DB value)."""
    try:
        server_config = patch_quasar({})
    except Exception as e:
        return {"ok": False, "err": f"quasar-server: {str(e)[:120]}"}
    desired = speaker_settings.resync_desired(server_config, speaker_settings.load_overlay())
    dev = speaker_settings.adb_read_device_config(ADB)
    drift = {k: v for k, v in desired.items()
             if dev.values.get(k) is not None and not speaker_settings.values_equal(v, dev.values[k])}
    if drift:
        try:
            speaker_settings.save_overlay_changes(drift)
            server_config = patch_quasar(speaker_settings.quasar_patch(drift))
        except Exception as e:
            return {"ok": False, "err": f"не удалось выровнять: {str(e)[:120]}"}
    return _verify_settings(desired, server_config, RESYNC_WAIT_S, {"ok": True, "converged": sorted(drift)})

def api_reboot():
    rc, out = sh(["adb", "-s", ADB, "reboot"], 20)
    return {"ok": rc == 0, "out": out[:160]}

def api_tts(text):
    text = (text or "").strip()[:500]
    if not text:
        return {"ok": False, "err": "пустой текст"}
    try:
        http_json(f"{QUASAR_API}/devices/{DUID}/push/text", "POST", {"eventText": text})
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "err": str(e)[:160]}

ADAPTER_URL = "http://127.0.0.1:31117"
FILLERS_LOCK = os.path.expanduser("~/.cache/aisis/gen_fillers.lock")
FILLERS_CMD = ["flock", FILLERS_LOCK, sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "gen_fillers.py")]

def tts_state():
    state = tts_settings.status()
    try:
        state["alice_last"] = http_json(ADAPTER_URL + "/healthz").get("alice_tts")
    except Exception:
        state["alice_last"] = None
    return state

def spawn_fillers():
    p = subprocess.Popen(FILLERS_CMD, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    threading.Thread(target=p.wait, daemon=True).start()   # reap: no defunct child per save

def tts_save(body):
    changes = {k: body[k] for k in ("tts_voice", "alice_engine", "alice_voice", "yandex_voice", "yandex_emotion",
        "yandex_folder_id") if k in body}
    clear = body.get("clear_key", False)
    key = body.get("yandex_key")
    if not isinstance(clear, bool):
        return {"ok": False, "err": "clear_key must be a boolean"}
    if key is not None and not isinstance(key, str):
        return {"ok": False, "err": "yandex_key must be a string"}
    before = (tts_settings.load(), tts_settings.voice_spec())
    try:
        new_key = tts_settings.normalize_key(key) if key and not clear else None  # validate all before persisting any
        if changes:
            tts_settings.update(changes)
        if clear:
            tts_settings.clear_key()
        elif new_key:
            tts_settings.write_key(new_key)
    except ValueError as e:
        return {"ok": False, "err": str(e)}
    # Fillers follow the effective voice; regenerate only when it or its settings changed.
    # flock serializes overlapping runs, kept files make a rerun cheap.
    if (tts_settings.load(), tts_settings.voice_spec()) != before:
        spawn_fillers()
    return {"ok": True, **tts_state()}

# ---------- Owner global prompt + tone preset (user_prompt.py) ----------
def user_prompt_state():
    """Saved settings, the shipped texts the UI offers, and the assembled prompt as the adapter sends it."""
    saved = user_prompt.load()
    base = user_prompt.base_persona_from_env_file()
    return {"ok": True, **saved, "max_chars": user_prompt.MAX_CHARS,
        "presets": [{"id": k, "label": v, "fragment": user_prompt.preset_fragment(k)} for k, v in user_prompt.PRESETS.items()],
        "profanity_fragments": {"on": user_prompt.profanity_fragment(True), "off": user_prompt.profanity_fragment(False)},
        "default": user_prompt.defaults(), "is_default": saved == user_prompt.defaults(),
        "base_persona": base, "assembled": user_prompt.assemble(base, saved)}

def user_prompt_save(body):
    try:
        if body.get("reset") is True:
            user_prompt.reset()
        else:
            user_prompt.save({k: body[k] for k in ("preset", "profanity", "text") if k in body})
    except ValueError as e:
        return {"ok": False, "err": str(e)}
    return user_prompt_state()

def user_prompt_preview(body):
    try:
        draft = user_prompt.validate({**user_prompt.load(), **{k: body[k] for k in ("preset", "profanity", "text") if k in body}})
    except ValueError as e:
        return {"ok": False, "err": str(e)}
    return {"ok": True, "assembled": user_prompt.assemble(user_prompt.base_persona_from_env_file(), draft)}

# ---------- TV pause on activation (tvpause.py) ----------
def tv_state():
    cfg = tvpause.load_settings()
    live = {}
    for entity in cfg["entities"]:
        state, stale = tvpause.HaCli().state(entity)
        live[entity] = None if state is None else (state + (" (кэш)" if stale else ""))
    st = tvpause.default().status()
    return {**cfg, "live": live, "paused": sorted(st["paused"]), "events": st["events"][-8:],
            "dry_run": os.environ.get("AISIS_TVPAUSE_DRY_RUN") == "1"}

def tv_save(body):
    try:
        tvpause.update_settings({k: body[k] for k in ("enabled", "resume") if k in body})
    except ValueError as e:
        return {"ok": False, "err": str(e)}
    return {"ok": True, **tv_state()}

# ---------- sound-start animation on the clock display (led_animation.py; the adapter fires it) ----------
def led_animation_state():
    return {**led_animation.load_settings(), "options": led_animation.options(),
            "min_ms": led_animation.MIN_DURATION_MS, "max_ms": led_animation.MAX_DURATION_MS}

def led_animation_save(body):
    try:
        led_animation.update_settings({k: body[k] for k in ("enabled", "animation", "duration_ms") if k in body})
    except ValueError as e:
        return {"ok": False, "err": str(e)}
    out = {"ok": True, **led_animation_state()}
    if body.get("test") is True:  # a button press, so a real push that reports its own failure
        settings = led_animation.load_settings()
        result = led_animation.play_now(settings["animation"], settings["duration_ms"], "dashboard")
        if not result["ok"]:
            return {**out, "ok": False, "err": result["err"]}
    return out

# ---------- dialog context for the brain prompt (dialog_context.py, settings read by the adapter) ----------
def dialog_context_state():
    import dialog_context
    return {"settings": dialog_context.load_settings(), "defaults": dict(dialog_context.DEFAULTS),
            "max": dialog_context.MAX_RECENT}

def dialog_context_save(body):
    import dialog_context
    try:
        dialog_context.update_settings({k: body[k] for k in dialog_context.DEFAULTS if k in body})
    except ValueError as e:
        return {"error": str(e)}
    return {"ok": True, **dialog_context_state()}

# ---------- owner voice (speakerid.py, scores written by the STT gateway) ----------
def voice_state():
    try:
        import speakerid
    except ImportError as e:
        return {"available": False, "err": f"speakerid недоступен: {e}"}
    conn = dialogstore.connect()
    try:
        stats = dialogstore.voice_stats(conn)
    finally:
        conn.close()
    return {"available": True, **speakerid.status(), "samples": speakerid.list_samples()[:40], "stats": stats}

def voice_save(body):
    import speakerid
    try:
        speakerid.update_settings({k: body[k] for k in ("mode", "threshold", "capture") if k in body})
    except ValueError as e:
        return {"ok": False, "err": str(e)}
    return {"ok": True, **voice_state()}

def voice_enrol(body):
    import speakerid
    ids = body.get("ids")
    if not isinstance(ids, list) or not ids:
        return {"ok": False, "err": "выберите образцы"}
    name = str(body.get("name", "")).strip()
    if not re.fullmatch(r"[\w .-]{1,40}", name):
        return {"ok": False, "err": "имя: до 40 символов, буквы/цифры/пробел/точка/дефис"}
    try:
        speakerid.enrol_from_samples(name, [str(i) for i in ids])
    except (ValueError, KeyError, RuntimeError, OSError) as e:
        return {"ok": False, "err": str(e)[:200]}
    return {"ok": True, **voice_state()}

def voice_delete(body):
    import speakerid
    try:
        if body.get("sample"):
            speakerid.delete_sample(str(body["sample"]))
        if body.get("profile"):
            speakerid.delete_profile(str(body["profile"]))
    except (ValueError, KeyError, OSError) as e:
        return {"ok": False, "err": str(e)[:200]}
    return {"ok": True, **voice_state()}

def voice_sample_wav(sample_id):
    import speakerid
    try:
        path = speakerid.sample_wav_path(sample_id)
        return path.read_bytes() if path else None
    except (ValueError, KeyError, OSError):
        return None

# ---------- services ----------
def parse_env_file(text):
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip("'\"")
    return out

def bluegreen_env():
    try:
        with open(BLUEGREEN_STATE, encoding="utf-8") as fh:
            return parse_env_file(fh.read())
    except OSError:
        return {}

def docker_states(names):
    """{name: (running, started, health, image)} from one `docker inspect`; missing containers are absent."""
    fmt = "{{.Name}}|{{.State.Running}}|{{.State.StartedAt}}|{{if .State.Health}}{{.State.Health.Status}}{{end}}|{{.Config.Image}}"
    rc, out = sh(["docker", "inspect", "-f", fmt, *names], 20)
    res = {}
    for line in out.splitlines():
        parts = line.split("|")
        if len(parts) == 5 and parts[0].startswith("/"):
            res[parts[0][1:]] = (parts[1] == "true", parts[2][:19].replace("T", " "), parts[3], parts[4])
    return res

def systemd_names():
    """Every existing aisis-*.service (user units); the static table when systemd can't be listed."""
    rc, out = sh(["systemctl", "--user", "list-unit-files", "aisis-*.service", "--no-legend", "--plain"], 10)
    found = sorted({l.split()[0] for l in out.splitlines() if l.split() and re.fullmatch(r"aisis-[\w.@-]+\.service", l.split()[0])})
    return found if rc == 0 and found else sorted(SYSTEMD_META)

def services_state():
    env = bluegreen_env()
    active = env.get("PROD_ACTIVE")
    docker = docker_states(list(DOCKER_SVCS))
    res = []
    for name, (label, role) in DOCKER_SVCS.items():
        color = name.rsplit("-", 1)[-1] if name.startswith("aisis-quasar-prod-") else None
        if color:
            role = f"{role}: " + ("активный" if color == active else "резерв")
        running, started = docker.get(name, (False, "", "", ""))[:2]
        res.append({"type": "docker", "name": name, "label": label, "running": running, "started": started, "role": role})
    for name in systemd_names():
        label, role = SYSTEMD_META.get(name, (name.removesuffix(".service"), ""))
        rc, out = sh(["systemctl", "--user", "show", name, "-p", "ActiveState", "-p", "ActiveEnterTimestamp"])
        d = dict(l.split("=", 1) for l in out.splitlines() if "=" in l)
        res.append({"type": "systemd", "name": name, "label": label, "running": d.get("ActiveState") == "active",
                    "started": d.get("ActiveEnterTimestamp", ""), "role": role})
    return res

def bluegreen_state():
    env = bluegreen_env()
    docker = docker_states([f"aisis-quasar-prod-{c}" for c in BLUEGREEN_COLORS])
    colors = {}
    for c in BLUEGREEN_COLORS:
        running, _, health, image = docker.get(f"aisis-quasar-prod-{c}", (False, "", "", ""))
        colors[c] = {"running": running, "healthy": health == "healthy",
                     "image": image or env.get(f"PROD_IMG_{c.upper()}")}
    return {"active": env.get("PROD_ACTIVE"), "colors": colors}

def yse_state():
    return yse.yse_state(ADB, speaker_settings.adb_online(ADB))

def service_restart(stype, name):
    if stype == "docker":
        if name not in DOCKER_SVCS:
            return {"ok": False, "err": "не разрешён"}
        rc, out = sh(["docker", "restart", name], 60)
        return {"ok": rc == 0, "out": out[:120]}
    if stype == "systemd":
        if name not in systemd_names():
            return {"ok": False, "err": "не разрешён"}
        if name == SELF_UNIT:
            # Restarting ourselves kills this process: answer first, restart from a detached shell.
            subprocess.Popen(["sh", "-c", f"sleep 1; systemctl --user restart {SELF_UNIT}"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            return {"ok": True, "out": "перезапуск через 1 с"}
        rc, out = sh(["systemctl", "--user", "restart", name], 60)
        return {"ok": rc == 0, "out": out[:120]}
    return {"ok": False, "err": "тип?"}

def corpus_state():
    if not os.path.isdir(CORPUS_DIR):
        return {"available": False, "note": "корпус ещё не создан (IntentCorpus работает)"}
    files = sorted(f for f in os.listdir(CORPUS_DIR) if not f.startswith("."))
    cats, kinds, total = {}, {}, 0
    jl = os.path.join(CORPUS_DIR, "intents-ru.jsonl")
    if os.path.isfile(jl):
        with open(jl, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line).get("expect", {})
                    dom, kind = e.get("domain", "?"), e.get("kind", "?")
                except Exception:
                    dom, kind = "?", "?"
                cats[dom] = cats.get(dom, 0) + 1
                kinds[kind] = kinds.get(kind, 0) + 1
                total += 1
    return {"available": True, "files": files, "categories": cats, "kinds": kinds,
            "total": total, "lastRun": _corpus_run.get("res")}

_corpus_run = {"t": 0, "res": None}

def corpus_container():
    """Stage quasar first (prod containers stay untouched by `docker cp`), then any running prod color."""
    names = ["aisis-quasar-stage-green", "aisis-quasar-stage-blue", "aisis-quasar-prod-green", "aisis-quasar-prod-blue"]
    states = docker_states(names)
    return next((n for n in names if states.get(n, (False,))[0]), None)

def corpus_run():
    if not os.path.isfile(os.path.join(CORPUS_DIR, "intents-ru.jsonl")):
        return {"ok": False, "err": "корпуса нет"}
    if time.time() - _corpus_run["t"] < 30 and _corpus_run["res"]:
        return _corpus_run["res"]
    box = corpus_container()
    if not box:
        return {"ok": False, "err": "нет запущенного quasar-контейнера для прогона"}
    sh(["docker", "exec", box, "mkdir", "-p", "/src/backend/corpus"], 15)
    rc, out = sh(["docker", "cp", CORPUS_DIR + "/.", f"{box}:/src/backend/corpus/"], 30)
    if rc != 0:
        return {"ok": False, "err": f"docker cp: {out[:120]}"}
    rc, out = sh(["docker", "exec", "-w", "/src/backend/corpus", box,
                  "/node_modules/.bin/tsx", "run_corpus.mts"], 120)
    if rc != 0:
        return {"ok": False, "err": out[-200:]}
    total_m = re.search(r"ИТОГ: (\d+)/(\d+) \(([\d.]+)%\)", out)
    doms = []
    for m in re.finditer(r"^(\S+)\s+(\d+)\s+(\d+)\s+(\d+)%", out, re.M):
        doms.append({"domain": m.group(1), "pass": int(m.group(2)),
                     "fail": int(m.group(3)), "rate": int(m.group(4))})
    fails = [l.strip() for l in out.splitlines() if "✗" in l]
    res = {"ok": True, "summary": total_m.groups() if total_m else None,
           "domains": doms, "fails": fails[:40], "at": time.strftime("%H:%M:%S")}
    _corpus_run.update(t=time.time(), res=res)
    return res

OPENCLAW_URL = "http://192.168.0.56:18789"
OPENCLAW_ENV = os.path.expanduser(
    "~/xp/aisis/.worktrees/claude-primary-api/backend/openclaw/.env")

def wakeword_state():
    url = "http://192.168.0.148:8780/"
    st = {"url": url, "ok": False}
    try:
        with urllib.request.urlopen(url, timeout=4) as r:
            st.update(ok=r.status == 200, status=r.status)
    except Exception as e:
        st["err"] = str(e)[:120]
    return st

def openclaw_state():
    st = {"url": OPENCLAW_URL, "deployed": False}
    try:
        with urllib.request.urlopen(f"{OPENCLAW_URL}/healthz", timeout=3) as r:
            st["healthz"] = r.status
            st["deployed"] = r.status == 200
    except Exception as e:
        st["err"] = str(e)[:120]
        return st
    tok = None
    try:
        with open(OPENCLAW_ENV, encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("OPENCLAW_GATEWAY_TOKEN="):
                    tok = line.split("=", 1)[1].strip()
                    break
    except Exception:
        tok = None
    if not tok:
        return st
    for path in ("/api/channels", "/api/toasts", "/api/messages"):
        try:
            st[path] = http_json(f"{OPENCLAW_URL}{path}",
                                 headers={"Authorization": f"Bearer {tok}"}, timeout=4)
        except Exception:
            pass
    return st

# ---------- static SPA (built by dashboard-ui: `bun run build`) ----------
HERE = os.path.dirname(os.path.abspath(__file__))
MISSING_UI = "UI не собран: cd dashboard-ui && bun run build"
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8", ".map": "application/json; charset=utf-8",
    ".svg": "image/svg+xml", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".webp": "image/webp", ".ico": "image/x-icon", ".txt": "text/plain; charset=utf-8",
    ".woff": "font/woff", ".woff2": "font/woff2", ".ttf": "font/ttf", ".wasm": "application/wasm",
    ".webmanifest": "application/manifest+json",
}

def dist_dir():
    return os.environ.get("AISIS_DASHBOARD_DIST") or os.path.join(HERE, "dashboard-ui", "dist")

def resolve_static(dist, url_path):
    """File under `dist` for a URL path, or None (missing, a directory, or an escape attempt)."""
    rel = unquote(url_path).lstrip("/")
    if not rel or "\x00" in rel:
        return None
    root = os.path.realpath(dist)
    full = os.path.realpath(os.path.join(root, rel))
    if os.path.commonpath([root, full]) != root or not os.path.isfile(full):
        return None
    return full

def static_cache_control(url_path, full):
    if url_path.startswith("/assets/"):
        return "public, max-age=31536000, immutable"
    return "no-store"

def is_spa_route(path):
    return not (path == "/events.json" or path == "/api" or path.startswith("/api/")
                or path == "/yse" or path.startswith("/yse/"))

# ---------- music / AirPlay (music_airplay.py, wired by lazy import) ----------
AIRPLAY_KEYS = ("enabled", "name", "volume_db", "output_delay_ms", "restart", "bluetooth_pairing", "bluetooth_seconds")

def music_call(fn_name, *args):
    try:
        import music_airplay
    except ImportError as e:
        return {"available": False, "err": f"music_airplay недоступен: {e}"}
    fn = getattr(music_airplay, fn_name, None)
    if fn is None:
        return {"available": False, "err": f"music_airplay.{fn_name} не реализовано"}
    try:
        return fn(*args)
    except Exception as e:
        return {"ok": False, "err": str(e)[:200]}

def music_play_route(body):
    query = body.get("query")
    if not isinstance(query, str) or not query.strip():
        return {"ok": False, "err": "query: нужна непустая строка"}
    media_type = body.get("media_type")
    if media_type is not None and not isinstance(media_type, str):
        return {"ok": False, "err": "media_type: нужна строка"}
    return music_call("music_play", query.strip()[:200], *([media_type] if media_type else []))

def music_control_route(body):
    action = body.get("action")
    if not isinstance(action, str) or not action:
        return {"ok": False, "err": "action: нужна строка"}
    return music_call("music_control", action, body.get("value"))

def airplay_set_route(body):
    changes = {k: body[k] for k in AIRPLAY_KEYS if k in body}
    return music_call("airplay_set", changes)

class H(BaseHTTPRequestHandler):
    def _send(self, code, ctype, body, cache="no-store"):
        if isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", cache)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, d, code=200):
        self._send(code, "application/json; charset=utf-8", json.dumps(d, ensure_ascii=False))

    def _dicts(self, method):
        """/api/dicts/*: stt-cli vocabulary management (dashboard_dicts.py); body is optional JSON."""
        u = urlparse(self.path)
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n).decode()) if n else {}
        except Exception:
            return self._json({"ok": False, "err": "bad json"}, 400)
        if not isinstance(body, dict):
            return self._json({"ok": False, "err": "тело должно быть JSON-объектом"}, 400)
        status, ctype, text = dashboard_dicts.route(method, u.path, parse_qs(u.query), body)
        self._send(status, ctype, text)

    def _serve_clip(self, clip_id):
        path = voiceclips.clip_file(dialogstore.DB_PATH, clip_id)
        if path is None:
            return self._json({"err": "404"}, 404)
        size = path.stat().st_size
        rng = parse_range(self.headers.get("Range"), size)
        if rng is False:
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{size}")
            self.send_header("Content-Length", "0")
            return self.end_headers()
        start, end = rng if rng else (0, size - 1)
        self.send_response(206 if rng else 200)
        self.send_header("Content-Type", CLIP_TYPES.get(path.suffix.lower(), "application/octet-stream"))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "private, max-age=3600")
        if rng:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Content-Length", str(end - start + 1))
        self.end_headers()
        with open(path, "rb") as fh:
            fh.seek(start)
            left = end - start + 1
            while left > 0:
                chunk = fh.read(min(65536, left))
                if not chunk:
                    break
                self.wfile.write(chunk)
                left -= len(chunk)

    def _serve_static(self, p):
        dist = dist_dir()
        index = resolve_static(dist, "index.html")
        if index is None:
            return self._send(503, "text/plain; charset=utf-8", MISSING_UI)
        full = resolve_static(dist, p)
        if full is None:
            if p.startswith("/assets/"):
                return self._json({"err": "404"}, 404)
            full = index
        ext = os.path.splitext(full)[1].lower()
        with open(full, "rb") as fh:
            data = fh.read()
        self._send(200, CONTENT_TYPES.get(ext, "application/octet-stream"), data,
                   static_cache_control(p, full) if full != index else "no-store")

    def _proxy_yse(self, method):
        try:
            res = yse.split_proxy_path(self.path)
        except ValueError:
            return self._json({"ok": False, "err": "bad path"}, 400)
        if res is None:
            return self._json({"ok": False, "err": "неизвестная панель"}, 404)
        panel, rest = res
        if rest is None:
            self.send_response(302)
            self.send_header("Location", f"/yse/{panel['id']}/")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = None
        if method in ("POST", "PUT"):
            n = int(self.headers.get("Content-Length") or 0)
            if n > yse.MAX_REQUEST_BODY:
                return self._json({"ok": False, "err": "тело слишком большое"}, 413)
            body = self.rfile.read(n) if n else b""
        try:
            conn, resp = yse.open_upstream(panel, method, rest, body, self.headers.get("Content-Type"))
        except Exception as e:
            return self._json({"ok": False, "err": f"панель {panel['id']} недоступна: {str(e)[:120]}"}, 502)
        try:
            ctype = resp.getheader("Content-Type")
            headers = yse.response_headers(resp.getheaders(), panel["id"])
            if not any(k.lower() == "cache-control" for k, _ in headers):
                headers.append(("Cache-Control", "no-store"))
            if yse.is_html(ctype):
                try:
                    data = resp.read(yse.MAX_HTML_BYTES + 1)
                except Exception as e:
                    return self._json({"ok": False, "err": f"панель оборвала ответ: {str(e)[:120]}"}, 502)
                data = yse.inject_shim(data[:yse.MAX_HTML_BYTES], panel["id"])
                self.send_response(resp.status)
                for k, v in headers:
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            self.send_response(resp.status)
            for k, v in headers:
                self.send_header(k, v)
            length = resp.getheader("Content-Length")
            if length:
                self.send_header("Content-Length", length)
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            try:
                while True:
                    chunk = resp.read(16384)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
            except (OSError, http.client.HTTPException):
                pass
        finally:
            conn.close()

    def do_GET(self):
        try:
            self._route_get()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            self._json({"ok": False, "err": f"внутренняя ошибка: {str(e)[:160]}"}, 500)

    def _route_get(self):
        u = urlparse(self.path)
        p, qs = u.path, parse_qs(u.query)
        if p == "/yse" or p.startswith("/yse/"):
            return self._proxy_yse("GET")
        if p == "/api/dicts" or p.startswith("/api/dicts/"):
            return self._dicts("GET")
        if p in ("/events.json", "/api/events"):
            try:
                limit = int(qs.get("limit", ["50"])[0])
                before = float(qs["before"][0]) if "before" in qs else None
                q = qs.get("q", [""])[0].strip() or None
                since = qs.get("since", [""])[0].strip() or None
                until = qs.get("until", [""])[0].strip() or None
                dialogstore._bound(since, False), dialogstore._bound(until, True)
            except ValueError:
                return self._json({"error": "bad limit/before/since/until"}, 400)
            d = events_json(limit, before, q, since, until)
            d["updated"] = time.strftime("%H:%M:%S")
            self._json(d)
        elif p == "/api/health":
            self._json({"ok": True, "version": VERSION, "uptimeS": int(time.time() - STARTED)})
        elif p == "/api/speaker":
            self._json(speaker_state())
        elif p == "/api/speaker/settings":
            self._json(speaker_settings_state())
        elif p == "/api/dialogs/stats":
            self._json(dialogs_stats())
        elif p == "/api/services":
            self._json(services_state())
        elif p == "/api/bluegreen":
            self._json(bluegreen_state())
        elif p == "/api/yse":
            self._json(yse_state())
        elif p == "/api/music":
            self._json(music_call("music_state"))
        elif p == "/api/airplay":
            self._json(music_call("airplay_state"))
        elif p == "/api/corpus":
            self._json(corpus_state())
        elif p == "/api/openclaw":
            self._json(openclaw_state())
        elif p == "/api/wakeword":
            self._json(wakeword_state())
        elif p == "/api/tts/settings":
            self._json(tts_state())
        elif p == "/api/persona/user-prompt":
            self._json(user_prompt_state())
        elif p == "/api/tv-pause":
            self._json(tv_state())
        elif p == "/api/led-animation":
            self._json(led_animation_state())
        elif p == "/api/dialog-context":
            self._json(dialog_context_state())
        elif p == "/api/voice":
            self._json(voice_state())
        elif p == "/api/voice/archive":
            self._json(voice_archive_state())
        elif p == "/api/asr/bench":
            self._json(asr_bench_state())
        elif p == "/api/clipfeed":
            self._json(clipfeed_state())
        elif p == "/api/clips/truth":
            self._json(clip_truth_state(qs))
        elif p == "/api/stt/tuning":
            self._json(stt_tuning.state())
        elif p == "/api/stt/dsp":
            self._json(dsp_settings.state(dialogstore.DB_PATH))
        elif p.startswith("/api/voice/clip/"):
            self._serve_clip(unquote(p[len("/api/voice/clip/"):]))
        elif p.startswith("/api/voice/sample/") and p.endswith(".wav"):
            wav = voice_sample_wav(unquote(p[len("/api/voice/sample/"):-4]))
            if wav is None:
                return self._json({"err": "404"}, 404)
            self._send(200, "audio/wav", wav)
        elif is_spa_route(p):
            self._serve_static(p)
        else:
            self._json({"err": "404"}, 404)

    def do_DELETE(self):
        try:
            if self.path == "/yse" or self.path.startswith("/yse/"):
                return self._proxy_yse("DELETE")
            if self.path.split("?")[0].startswith("/api/dicts/"):
                return self._dicts("DELETE")
            self._json({"ok": False, "err": "404"}, 404)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            self._json({"ok": False, "err": f"внутренняя ошибка: {str(e)[:160]}"}, 500)

    def do_PUT(self):
        try:
            if self.path.split("?")[0].startswith("/api/dicts/"):
                return self._dicts("PUT")
            self._json({"ok": False, "err": "404"}, 404)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            self._json({"ok": False, "err": f"внутренняя ошибка: {str(e)[:160]}"}, 500)

    def do_POST(self):
        try:
            self._route_post()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            self._json({"ok": False, "err": f"внутренняя ошибка: {str(e)[:160]}"}, 500)

    def _route_post(self):
        if self.path == "/yse" or self.path.startswith("/yse/"):
            return self._proxy_yse("POST")
        if self.path.split("?")[0].startswith("/api/dicts/"):
            return self._dicts("POST")
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n).decode()) if n else {}
        except Exception:
            return self._json({"ok": False, "err": "bad json"}, 400)
        if not isinstance(body, dict):
            return self._json({"ok": False, "err": "тело должно быть JSON-объектом"}, 400)
        p = self.path.split("?")[0]
        if p == "/api/speaker/volume":
            return self._json(api_volume(body))
        if p == "/api/speaker/mute":
            return self._json(api_mute(body.get("muted")))
        if p == "/api/speaker/settings":
            return self._json(speaker_settings_apply(body))
        if p == "/api/speaker/settings/resync":
            return self._json(speaker_settings_resync())
        if p == "/api/speaker/reboot":
            return self._json(api_reboot())
        if p == "/api/speaker/tts":
            return self._json(api_tts(body.get("text")))
        if p == "/api/service/restart":
            return self._json(service_restart(body.get("type"), body.get("name")))
        if p == "/api/corpus/run":
            return self._json(corpus_run())
        if p == "/api/music/play":
            return self._json(music_play_route(body))
        if p == "/api/music/control":
            return self._json(music_control_route(body))
        if p == "/api/airplay":
            return self._json(airplay_set_route(body))
        if p == "/api/tts/settings":
            return self._json(tts_save(body))
        if p == "/api/persona/user-prompt":
            return self._json(user_prompt_save(body))
        if p == "/api/persona/user-prompt/preview":
            return self._json(user_prompt_preview(body))
        if p == "/api/tv-pause":
            return self._json(tv_save(body))
        if p == "/api/led-animation":
            return self._json(led_animation_save(body))
        if p == "/api/dialog-context":
            res = dialog_context_save(body)
            return self._json(res, 400 if "error" in res else 200)
        if p == "/api/voice":
            return self._json(voice_save(body))
        if p == "/api/voice/enrol":
            return self._json(voice_enrol(body))
        if p == "/api/voice/delete":
            return self._json(voice_delete(body))
        if p == "/api/clipfeed":
            return self._json(clipfeed_save(body))
        if p == "/api/clips/truth":
            return self._json(clip_truth_save(body))
        if p == "/api/stt/tuning":
            return self._json(stt_tuning_save(body))
        if p == "/api/stt/dsp":
            return self._json(stt_dsp_save(body))
        self._json({"ok": False, "err": "404"}, 404)

    def log_message(self, *a):
        pass

if __name__ == "__main__":
    tv = tvpause.default()
    tv.recover()
    INGESTER = dialogstore.Ingester(on_log=tv.observe)
    INGESTER.start()
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()

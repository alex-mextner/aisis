#!/usr/bin/env python3
"""Local deployment/synthetic probes. Never prints credential or speaker inventory."""
from __future__ import annotations
import argparse
import asyncio
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import time

from aiohttp import ClientSession, ClientTimeout


def load_env():
    path = Path.home()/".config/aisis-yandex/runtime.env"
    for line in path.read_text().splitlines():
        if not line or line.lstrip().startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        parts = shlex.split(value, comments=False)
        os.environ[name] = " ".join(parts)
    return path


def start(config):
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    units = ["aisis-yandex-wyoming-tunnel.service", "aisis-yandex-pm.service", "aisis-yandex-stt.service", "aisis-yandex-adapter.service"]
    subprocess.run(["systemctl", "--user", "enable", "--now", *units], check=True)
    compose = Path(__file__).parent/"compose.yaml"
    argv = ["docker", "compose", "--env-file", str(config), "-f", str(compose), "up", "-d", "postgres", "quasar"]
    subprocess.run(["ssh", "-oBatchMode=yes", "-oStrictHostKeyChecking=yes", "localhost", shlex.join(argv)], check=True)
    for unit in units:
        result = subprocess.run(["systemctl", "--user", "show", unit, "-p", "ActiveState", "-p", "SubState", "-p", "Result"], capture_output=True, text=True, check=True)
        print(json.dumps({"unit": unit, "state": result.stdout.splitlines()}))


async def probe(args):
    results = {}
    async with ClientSession(timeout=ClientTimeout(total=60)) as session:
        async def check(name, url):
            try:
                async with session.get(url, timeout=ClientTimeout(total=2)) as response:
                    body = await response.json()
                    value = {"http": response.status}
                    if name == "quasar":
                        value["registered_devices"] = len(body) if isinstance(body, list) else None
                    else:
                        value.update(body)
                    return name, value
            except Exception as exc:
                return name, {"error": type(exc).__name__}
        endpoints = {"adapter": "http://127.0.0.1:31117/healthz", "pm": "http://127.0.0.1:31118/health", "stt": "http://127.0.0.1:31121/healthz", "quasar": "http://127.0.0.1:31116/devices"}
        deadline = time.monotonic() + (20 if args.start else 2)
        while True:
            results = dict(await asyncio.gather(*(check(name, url) for name, url in endpoints.items())))
            healthy = all(value.get("http") == 200 for value in results.values())
            healthy = healthy and results["adapter"].get("processor_configured") and results["adapter"].get("tts_configured") and results["stt"].get("configured") and results["pm"].get("ready")
            if healthy or time.monotonic() >= deadline:
                break
            await asyncio.sleep(.5)
        print(json.dumps({"health": results}, ensure_ascii=False), flush=True)
        if not healthy:
            raise RuntimeError("required_services_unhealthy")
        if args.speech:
            text = "Расскажи, почему осенью листья меняют цвет."
            async with session.post("http://127.0.0.1:31117/v1/audio/speech", json={"input": text, "voice": "Брачо", "response_format": "opus"}) as response:
                if response.status != 200:
                    raise RuntimeError("tts_probe_http_" + str(response.status))
                ogg = await response.read()
            if not ogg.startswith(b"OggS") or b"OpusHead" not in ogg[:256]:
                raise RuntimeError("tts_probe_invalid_opus")
            state = Path.home()/".local/state/aisis-yandex"
            state.mkdir(parents=True, exist_ok=True, mode=0o700)
            file = state/"bracho-stt-probe.ogg"
            file.write_bytes(ogg); file.chmod(0o600)
            proc = await asyncio.create_subprocess_exec(os.environ.get("FFMPEG", "ffmpeg"), "-hide_banner", "-loglevel", "error", "-i", "pipe:0", "-ac", "1", "-ar", "16000", "-f", "s16le", "pipe:1", stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            pcm, _ = await asyncio.wait_for(proc.communicate(ogg), 30)
            if proc.returncode or not pcm:
                raise RuntimeError("tts_probe_decode_failed")
            print(json.dumps({"tts": "ok", "ogg_bytes": len(ogg), "pcm_seconds": len(pcm)/32000}, ensure_ascii=False), flush=True)
            ws = await session.ws_connect("http://127.0.0.1:31121/", timeout=10)
            try:
                await ws.send_json({"sample_rate": 16000})
                audio = pcm + bytes(32000)
                for offset in range(0, len(audio), 3200):
                    await ws.send_bytes(audio[offset:offset+3200])
                answer = await ws.receive_json(timeout=40)
                print(json.dumps({"stt_expected": text, "stt_actual": answer}, ensure_ascii=False), flush=True)
                if not answer.get("end_of_utt") or not answer.get("text", "").strip():
                    raise RuntimeError("stt_probe_empty")
            finally:
                await ws.close()
        if args.chatgpt:
            # Both turns are explicitly synthetic; no prior user tabs are used.
            import uuid
            headers = {"Authorization": "Bearer " + os.environ["AISIS_PM_TOKEN"]}
            conversation = None
            for text in ["Запомни кодовое слово «маяк». Ответь только: запомнил.", "Какое кодовое слово я только что назвал? Ответь только этим словом."]:
                async with session.post("http://127.0.0.1:31118/turn", headers=headers, json={"station_id": next(x for x in os.environ["AISIS_ALLOWED_STATIONS"].split(",") if x), "text": text, "conversation_id": conversation, "turn_id": uuid.uuid4().hex}, timeout=ClientTimeout(total=180)) as response:
                    body = await response.json()
                    if response.status != 200:
                        print(json.dumps({"pm_probe_http": response.status, "result": body}, ensure_ascii=False), flush=True)
                        raise RuntimeError("pm_probe_failed_no_retry")
                conversation = body["conversation_id"]
                print(json.dumps({"pm_reply": body["text"], "conversation_received": bool(conversation)}, ensure_ascii=False), flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", action="store_true")
    parser.add_argument("--speech", action="store_true")
    parser.add_argument("--chatgpt", action="store_true")
    args = parser.parse_args()
    config = load_env()
    if args.start:
        start(config)
    asyncio.run(probe(args))


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""One-turn client for an existing ChatGPT-web OpenAI-compatible bridge.

No pm 'ask' command exists. The configured endpoint must be the actual browser
bridge, never OpenAI's paid API. HTTP failures and timeouts are never retried.
Local histories let bridges with message-prefix conversation matching preserve
per-speaker continuity. Pending turns require explicit local reconciliation.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
from urllib.parse import urlsplit

from aiohttp import ClientSession, ClientTimeout


async def run(payload):
    direct = os.environ.get("AISIS_PM_URL", "")
    if direct:
        parsed = urlsplit(direct)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"} or parsed.username or parsed.password:
            raise RuntimeError("local_pm_bridge_required")
        token = os.environ.get("AISIS_PM_TOKEN", "")
        if len(token) < 24:
            raise RuntimeError("pm_bridge_token_missing")
        async with ClientSession(timeout=ClientTimeout(total=float(os.environ.get("AISIS_CHATGPT_TIMEOUT", "180")))) as session:
            async with session.post(direct, headers={"Authorization": "Bearer " + token}, json=payload, allow_redirects=False) as response:
                if response.status != 200:
                    raise RuntimeError("pm_bridge_http_" + str(response.status))
                result = await response.json()
        if not isinstance(result, dict) or not isinstance(result.get("text"), str) or not result["text"].strip():
            raise RuntimeError("pm_bridge_invalid_answer")
        return {"text": result["text"], "conversation_id": result.get("conversation_id")}
    endpoint = os.environ.get("AISIS_CHATGPT_WEB_URL", "")
    parsed = urlsplit(endpoint)
    # Service access is local only. Browser authentication stays in its owner.
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"} or parsed.username or parsed.password:
        raise RuntimeError("existing_local_chatgpt_web_bridge_required")
    station = payload["station_id"]
    if not isinstance(station, str) or not station:
        raise ValueError("missing_station")
    identity = hashlib.sha256(station.encode()).hexdigest()
    path = Path(os.environ.get("AISIS_CHATGPT_HISTORY", str(Path.home()/".local/state/aisis-yandex/chatgpt-history.sqlite3")))
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    os.chmod(path, 0o600)
    try:
        db.execute("CREATE TABLE IF NOT EXISTS conversations (id TEXT PRIMARY KEY, history TEXT, pending TEXT)")
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT history,pending FROM conversations WHERE id=?", (identity,)).fetchone()
        if row and row[1]:
            raise RuntimeError("previous_turn_pending_inspect_bridge_before_reconciliation")
        if payload.get("conversation_id") not in (None, identity):
            raise RuntimeError("speaker_conversation_mismatch")
        history = json.loads(row[0]) if row else []
        messages = history + [{"role": "user", "content": payload["text"]}]
        db.execute("INSERT INTO conversations VALUES (?,?,?) ON CONFLICT(id) DO UPDATE SET pending=excluded.pending",
            (identity, json.dumps(history, ensure_ascii=False), payload["turn_id"]))
        db.commit()  # Reservation survives timeout or cancellation. Never automatic replay.
        key = os.environ.get("AISIS_CHATGPT_WEB_API_KEY", "")
        headers = {"Authorization": "Bearer " + key} if key else {}
        async with ClientSession(timeout=ClientTimeout(total=float(os.environ.get("AISIS_CHATGPT_TIMEOUT", "180")))) as session:
            async with session.post(endpoint, headers=headers, allow_redirects=False, json={
                "model": os.environ.get("AISIS_CHATGPT_WEB_MODEL", "chatgpt-web"),
                "messages": messages, "stream": False,
            }) as response:
                if response.status != 200:
                    # A rejection may happen before or after a send; keep pending.
                    raise RuntimeError("chatgpt_web_http_" + str(response.status))
                result = await response.json()
        text = result["choices"][0]["message"]["content"]
        if not isinstance(text, str) or not text.strip():
            raise RuntimeError("chatgpt_web_invalid_answer")
        history = messages + [{"role": "assistant", "content": text}]
        db.execute("UPDATE conversations SET history=?,pending=NULL WHERE id=? AND pending=?",
            (json.dumps(history, ensure_ascii=False), identity, payload["turn_id"]))
        db.commit()
        return {"text": text, "conversation_id": identity}
    finally:
        db.close()


if __name__ == "__main__":
    try:
        payload = json.load(sys.stdin)
        result = asyncio.run(run(payload))
        print(json.dumps(result, ensure_ascii=False))
    except Exception as exc:
        # Do not print request text, endpoint credentials or API response content.
        print("chatgpt_bridge_failed:" + type(exc).__name__, file=sys.stderr)
        sys.exit(1)

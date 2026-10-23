#!/usr/bin/env python3
"""One-turn authenticated client for the existing pm browser adapter.

Conversation ownership and the text-free turn ledger belong to the persistent
server. This client stores no transcript and never retries a failed request.
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import re
import sys
from urllib.parse import urlsplit

from aiohttp import ClientSession, ClientTimeout


def http_timeout():
    value=float(os.environ.get("AISIS_PM_HTTP_TIMEOUT", "240"))
    if not math.isfinite(value) or value<=0 or value>300:
        raise ValueError("invalid_pm_http_timeout")
    return value


async def run(payload):
    direct=os.environ.get("AISIS_PM_URL", "")
    parsed=urlsplit(direct)
    if parsed.scheme!="http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"} or parsed.username or parsed.password:
        raise RuntimeError("local_pm_bridge_required")
    token=os.environ.get("AISIS_PM_TOKEN", "")
    if len(token)<24:
        raise RuntimeError("pm_bridge_token_missing")
    async with ClientSession(timeout=ClientTimeout(total=http_timeout())) as session:
        async with session.post(direct,headers={"Authorization":"Bearer "+token},json=payload,allow_redirects=False) as response:
            if response.status!=200:
                # Only a fixed short server code crosses the error boundary;
                # never include arbitrary response content or request text.
                code=None
                try:
                    result=await response.json()
                    if isinstance(result,dict):code=result.get("error")
                except (ValueError,TypeError):pass
                if isinstance(code,str) and re.fullmatch(r"[a-z][a-z0-9_]{0,63}",code):
                    raise RuntimeError("pm_bridge_"+code)
                raise RuntimeError("pm_bridge_http_"+str(response.status))
            result=await response.json()
    if not isinstance(result,dict) or not isinstance(result.get("text"),str) or not result["text"].strip():
        raise RuntimeError("pm_bridge_invalid_answer")
    return {"text":result["text"],"conversation_id":result.get("conversation_id")}


if __name__=="__main__":
    try:
        result=asyncio.run(run(json.load(sys.stdin)))
        print(json.dumps(result,ensure_ascii=False))
    except Exception as exc:
        print("chatgpt_bridge_failed:"+type(exc).__name__,file=sys.stderr)
        sys.exit(1)

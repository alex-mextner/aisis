#!/usr/bin/env python3
"""Bounded runtime probe. Never sends a ChatGPT prompt or contacts a speaker."""
import asyncio
import json
import socket
import sys

from aiohttp import ClientSession, ClientTimeout

async def main():
    result = {}
    async with ClientSession(timeout=ClientTimeout(total=5)) as session:
        for name, url in {
            "adapter": "http://127.0.0.1:31117/healthz",
            "quasar": "http://127.0.0.1:31116/devices",
        }.items():
            try:
                async with session.get(url) as response:
                    data = await response.json()
                    result[name] = {"status": response.status}
                    if name == "adapter":
                        result[name].update(data)
                    elif isinstance(data, list):
                        # Do not print actual station IDs/names/configuration.
                        result[name]["registered_devices"] = len(data)
            except Exception as exc:
                result[name] = {"error": type(exc).__name__}
    print(json.dumps(result, ensure_ascii=False))
    return 0 if all(x.get("status") == 200 for x in result.values()) else 1

if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

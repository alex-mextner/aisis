#!/usr/bin/env python3
"""Smoke checks for a quasar-server instance: HTTP health and a synthetic
uniproxy WebSocket handshake. Standard library only.

The handshake mimics what the speaker does first (GET /uni.ws with the
x-uprx-device-* headers, Upgrade to WebSocket) with a throwaway device id, then
exchanges a ping/pong and closes cleanly. It sends no protobuf messages, so no
dialog, directive or device action can result.

  smoke.py --api http://127.0.0.1:31516 --ws 127.0.0.1:31515
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import socket
import struct
import sys
import time
import urllib.error
import urllib.request


def http_status(url: str, timeout: float = 2.0) -> int:
    """Status code of GET url; 0 when nothing answers. Any HTTP reply counts as an answer."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except (urllib.error.URLError, OSError, ValueError):
        return 0


def http_json(url: str, timeout: float = 2.0):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            if resp.status != 200:
                return None
            return json.loads(resp.read())
    except (urllib.error.URLError, OSError, ValueError):
        return None


def _frame(opcode: int, payload: bytes = b"") -> bytes:
    mask = os.urandom(4)
    header = bytes([0x80 | opcode])
    if len(payload) < 126:
        header += bytes([0x80 | len(payload)])
    else:
        header += bytes([0x80 | 126]) + struct.pack(">H", len(payload))
    return header + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload))


def _read_exact(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("peer closed")
        buf += chunk
    return buf


def _read_frame(sock: socket.socket) -> tuple[int, bytes]:
    b0, b1 = _read_exact(sock, 2)
    length = b1 & 0x7F
    if length == 126:
        length = struct.unpack(">H", _read_exact(sock, 2))[0]
    elif length == 127:
        length = struct.unpack(">Q", _read_exact(sock, 8))[0]
    payload = _read_exact(sock, length) if length else b""
    return b0 & 0x0F, payload


def ws_open(host: str, port: int, path: str = "/uni.ws", timeout: float = 3.0) -> socket.socket:
    """Connect and complete the WebSocket upgrade with a throwaway device id; the caller closes the socket."""
    key = base64.b64encode(os.urandom(16)).decode()
    request = (
        f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n"
        f"x-uprx-device-id: bg-smoke-{os.urandom(4).hex()}\r\nx-uprx-device-model: bg-smoke\r\n\r\n"
    )
    sock = socket.create_connection((host, port), timeout=timeout)
    try:
        sock.settimeout(timeout)
        sock.sendall(request.encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = sock.recv(4096)
            if not chunk:
                raise ConnectionError("closed before upgrade response")
            head += chunk
            if len(head) > 16384:
                raise ConnectionError("oversized upgrade response")
        status_line = head.split(b"\r\n", 1)[0].decode("latin1")
        if " 101 " not in status_line:
            raise ConnectionError(f"upgrade refused: {status_line}")
    except BaseException:
        sock.close()
        raise
    return sock


def ws_handshake(host: str, port: int, path: str = "/uni.ws", hold: float = 1.0, timeout: float = 3.0) -> dict:
    """Upgrade, hold the socket open `hold` seconds, ping/pong, close. Raises on any failure."""
    started = time.monotonic()
    with ws_open(host, port, path, timeout) as sock:
        upgraded = time.monotonic() - started
        time.sleep(hold)
        sock.sendall(_frame(0x9, b"smoke"))
        pong = False
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            opcode, payload = _read_frame(sock)
            if opcode == 0xA and payload == b"smoke":
                pong = True
                break
            if opcode == 0x8:
                raise ConnectionError("server closed the socket during the hold")
        if not pong:
            raise ConnectionError("no pong")
        sock.sendall(_frame(0x8, struct.pack(">H", 1000)))
    return {"upgrade_s": round(upgraded, 3), "total_s": round(time.monotonic() - started, 3)}


def run(api: str, ws: str) -> dict:
    result: dict = {}
    devices = http_json(api.rstrip("/") + "/devices")
    result["api_devices_200"] = devices is not None
    host, _, port = ws.rpartition(":")
    try:
        result["ws"] = ws_handshake(host, int(port))
        result["ws_ok"] = True
    except (OSError, ConnectionError) as exc:
        result["ws_ok"] = False
        result["ws_error"] = f"{type(exc).__name__}: {exc}"
    result["ok"] = bool(result["api_devices_200"] and result["ws_ok"])
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--api", required=True, help="quasar API base URL")
    parser.add_argument("--ws", required=True, help="host:port serving /uni.ws")
    args = parser.parse_args()
    result = run(args.api, args.ws)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())

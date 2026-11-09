"""Speaker clock check for the failover watchdog. Standard library only.

The Mini 2's kernel clock is always UTC; what the LED shows is UTC plus the timezone offset
maind keeps in /data/quasar/data/geosync.state (protobuf: field 2 = {1: name, 2: offsetSeconds}),
written when quasar-server answers the device's timesync callback. That value drifted to UTC
once (station record default from a UTC container), so the speaker showed Belgrade time minus
one hour. This probe reads the state over adb and compares it, and the UTC epoch, with the host.
"""
from __future__ import annotations

import os
import subprocess
import time

ADB_TARGET = os.environ.get("AISIS_ADB", "192.168.0.148:5555")
GEOSYNC_STATE = "/data/quasar/data/geosync.state"
MAX_EPOCH_SKEW_S = 120


def _varint(data: bytes, pos: int) -> tuple[int, int]:
    value = shift = 0
    while True:
        byte = data[pos]
        pos += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, pos
        shift += 7


def _fields(data: bytes) -> list[tuple[int, int | bytes]]:
    """Top-level protobuf fields as (number, varint | length-delimited bytes); fixed64/32 are skipped."""
    pos, out = 0, []
    while pos < len(data):
        tag, pos = _varint(data, pos)
        number, wire = tag >> 3, tag & 7
        if wire == 0:
            value, pos = _varint(data, pos)
            out.append((number, value))
        elif wire == 2:
            length, pos = _varint(data, pos)
            out.append((number, data[pos:pos + length]))
            pos += length
        elif wire == 1:
            pos += 8
        elif wire == 5:
            pos += 4
        else:
            raise ValueError(f"unsupported wire type {wire}")
    return out


def parse_geosync_state(data: bytes) -> tuple[str, int]:
    """(timezone name, offset seconds) stored by maind; offset 0 when the field is absent (UTC)."""
    inner = next((value for number, value in _fields(data) if number == 2 and isinstance(value, bytes)), None)
    if inner is None:
        raise ValueError("no timezone field in geosync.state")
    name, offset = "", 0
    for number, value in _fields(inner):
        if number == 1 and isinstance(value, bytes):
            name = value.decode("utf-8", "replace")
        elif number == 2 and isinstance(value, int):
            offset = value
    return name, offset


def verdict(name: str, offset: int, device_epoch: int, host_epoch: float, host_offset: int) -> tuple[bool, str]:
    """(ok, detail): the speaker must be on the host's UTC offset and within MAX_EPOCH_SKEW_S of host time."""
    skew = int(device_epoch - host_epoch)
    problems = []
    if offset != host_offset:
        problems.append(f"часовой пояс колонки {name or '?'} {offset / 3600:+g} ч, у хоста {host_offset / 3600:+g} ч")
    if abs(skew) > MAX_EPOCH_SKEW_S:
        problems.append(f"UTC-часы колонки расходятся с хостом на {skew} с")
    detail = f"tz={name} offset={offset} host_offset={host_offset} skew_s={skew}"
    return not problems, "; ".join(problems) if problems else detail


def probe(timeout: float = 6.0) -> tuple[bool | None, str]:
    """(ok, detail) or (None, reason) when the speaker is unreachable over adb (not a clock fault)."""
    try:
        subprocess.run(["adb", "connect", ADB_TARGET], capture_output=True, timeout=timeout)
        result = subprocess.run(
            ["adb", "-s", ADB_TARGET, "shell", f"date +%s; xxd -p {GEOSYNC_STATE}"],
            capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, f"adb {type(exc).__name__}"
    host_epoch = time.time()
    lines = result.stdout.split()
    if result.returncode != 0 or len(lines) < 2 or not lines[0].isdigit():
        return None, f"adb rc={result.returncode} {result.stdout.strip()[:80]}"
    try:
        name, offset = parse_geosync_state(bytes.fromhex("".join(lines[1:])))
    except (ValueError, IndexError) as exc:
        return False, f"geosync.state не разобрать: {exc}"
    return verdict(name, offset, int(lines[0]), host_epoch, time.localtime(host_epoch).tm_gmtoff)

#!/usr/bin/env python3
"""Small fail-closed compatibility changes, applied only to the pinned source."""
import pathlib
import sys
import subprocess
import hashlib
import json

root = pathlib.Path(sys.argv[1])

def replace(path, old, new, count=1):
    p = root/path
    text = p.read_text()
    if text.count(old) != count:
        raise SystemExit(f"Pinned upstream mismatch: {path}")
    p.write_text(text.replace(old, new))

replace("src/routers/uniproxy.alice.yandex.net.ts",
    "const deviceId = String(request.headers['x-uprx-device-id']) ?? 'unknown'",
    "const deviceId = String(request.headers['x-uprx-device-id'] ?? '')\n    if (!deviceId) { websocket.close(1008, 'missing device id'); return }")
replace("src/routers/uniproxy.alice.yandex.net.ts",
    "new UniProxyConnection(websocket, parameters)",
    "new UniProxyConnection(websocket, { ...parameters, stationId: deviceId })")
replace("src/routers/uniproxy/uniproxy-connection.ts",
    "export interface UniProxyConnectionParameters {",
    "export interface UniProxyConnectionParameters {\n  stationId?: string")
replace("src/routers/uniproxy/uniproxy-connection.ts",
    "metadata: {}", "metadata: { stationId: this.parameters.stationId }", count=5)
replace("src/routers/uniproxy/uniproxy-connection.ts",
    "metadata: event.metadata,", "metadata: { ...event.metadata, stationId: this.parameters.stationId },")
replace("src/storage/types.ts",
    "network: z.boolean().default(true),\n      remoteActivation: z.boolean().default(true)",
    "network: z.boolean().default(false),\n      remoteActivation: z.boolean().default(false)")
replace("src/storage/types.ts",
    "forwarders: z.array(z.string()).default([\n        '77.88.8.1',\n        '77.88.8.8',\n        '77.88.8.88',\n        '77.88.8.2',\n        '1.1.1.1'\n      ])",
    "forwarders: z.array(z.string()).default([])")
replace("src/storage/types.ts", "enable: z.boolean().default(true),\n      forwarders:",
    "enable: z.boolean().default(false),\n      forwarders:")
replace("src/main.ts", "apiServer.listen(environment.API_PORT, () => {",
    "apiServer.listen({ port: environment.API_PORT, hostname: '127.0.0.1' }, () => {")
# Avoid sending voice transcripts and household metadata to a placeholder telemetry DSN.
replace("src/main.ts", "Sentry.init({", "Sentry.init({\n  enabled: false,")
print("Applied pinned Quasar station isolation, loopback admin and safe device defaults")

# Keep normal native EOF distinct from cancellation; see behavioral TS tests.
patch = pathlib.Path(__file__).with_name("quasar-native-eof.patch")
hashes = json.loads(patch.with_name("quasar-native-eof-hashes.json").read_text())
for relative, expected in hashes["original"].items():
    if hashlib.sha256((root/relative).read_bytes()).hexdigest() != expected:
        raise SystemExit(f"Pinned EOF preimage mismatch: {relative}")
# Zero-context diff avoids whitespace-only context lines in the public patch.
# Exact SHA256 preimages above make the source match fail closed.
subprocess.run(["git", "-C", str(root), "apply", "--unidiff-zero", "--check", str(patch.resolve())], check=True)
subprocess.run(["git", "-C", str(root), "apply", "--unidiff-zero", str(patch.resolve())], check=True)
for relative, expected in hashes["fixed"].items():
    if hashlib.sha256((root/relative).read_bytes()).hexdigest() != expected:
        raise SystemExit(f"Pinned EOF postimage mismatch: {relative}")
print("Applied bounded native speech EOF and exactly-once finalization")

# Reassemble complete Ogg packets, including page continuations, before Opus.
packet_patch = pathlib.Path(__file__).with_name("quasar-ogg-packets.patch")
packet_hashes = json.loads(packet_patch.with_name("quasar-ogg-packets-hashes.json").read_text())
for relative, expected in packet_hashes["original"].items():
    if hashlib.sha256((root/relative).read_bytes()).hexdigest() != expected:
        raise SystemExit(f"Pinned Ogg preimage mismatch: {relative}")
subprocess.run(["git", "-C", str(root), "apply", "--unidiff-zero", "--check", str(packet_patch.resolve())], check=True)
subprocess.run(["git", "-C", str(root), "apply", "--unidiff-zero", str(packet_patch.resolve())], check=True)
for relative, expected in packet_hashes["fixed"].items():
    if hashlib.sha256((root/relative).read_bytes()).hexdigest() != expected:
        raise SystemExit(f"Pinned Ogg postimage mismatch: {relative}")
print("Applied complete Ogg packet assembly before Opus decoding")

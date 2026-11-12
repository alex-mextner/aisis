#!/usr/bin/env python3
"""Summarize DHCP and speaker-DNS traffic from tcpdump pcap files (standard library only, read-only).

  summarize.py a.pcap [b.pcap ...]         one report per file (the file name is its label)
  summarize.py --selftest                    builds a synthetic pcap and checks the report

Per file: (1) which hosts answered DHCP (OFFER/ACK/NAK sender IP + MAC + server-identifier) and the DNS servers they
handed out (option 6), (2) every DHCP message with src/dst, client MAC, server-id, requested/offered address and option 6,
(3) the speaker's DNS queries per destination server with the names asked, and the A answers seen per server.
"""
import argparse
import collections
import datetime
import os
import socket
import struct
import sys
import tempfile

def _env(name, fallback):
    return os.environ.get(name) or fallback


# Role labels come from the private lan.env values, passed as environment variables by capture-both.sh. The fallbacks are
# documentation addresses so the module and its selftest never carry real inventory.
SPEAKER_IP = _env("AISIS_LAN_SPEAKER_ADDR", "192.0.2.30")
SPEAKER_MAC = _env("AISIS_LAN_SPEAKER_MAC", "02:00:00:00:00:30").lower()
FRONT_IP = _env("AISIS_LAN_FRONT_ADDR", "192.0.2.10")
BACKEND_IP = _env("AISIS_LAN_BACKEND_ADDR", "192.0.2.20")
GATEWAY_IP = _env("AISIS_LAN_GATEWAY", "192.0.2.1")
FRONT_MAC = _env("AISIS_LAN_FRONT_MAC", "02:00:00:00:00:10").lower()
BACKEND_MAC = _env("AISIS_LAN_BACKEND_MAC", "02:00:00:00:00:20").lower()
KNOWN_IPS = {FRONT_IP: "front", BACKEND_IP: "backend host", GATEWAY_IP: "gateway", SPEAKER_IP: "speaker"}
KNOWN_MACS = {FRONT_MAC: "front", BACKEND_MAC: "backend host", SPEAKER_MAC: "speaker"}
DHCP_TYPES = {1: "DISCOVER", 2: "OFFER", 3: "REQUEST", 4: "DECLINE", 5: "ACK", 6: "NAK", 7: "RELEASE", 8: "INFORM"}
QTYPES = {1: "A", 28: "AAAA", 5: "CNAME", 15: "MX", 16: "TXT", 12: "PTR", 65: "HTTPS", 64: "SVCB", 2: "NS", 6: "SOA"}


def label_ip(ip):
    return f"{ip} ({KNOWN_IPS[ip]})" if ip in KNOWN_IPS else ip


def label_mac(mac):
    return f"{mac} ({KNOWN_MACS[mac]})" if mac in KNOWN_MACS else mac


def mac_str(raw):
    return ":".join(f"{b:02x}" for b in raw)


# ---- pcap / frame parsing ----------------------------------------------------------------------------------------
def read_pcap(path):
    """Yield (timestamp, linktype, frame). A truncated last record (tcpdump killed) is ignored."""
    with open(path, "rb") as handle:
        data = handle.read()
    if len(data) < 24:
        raise ValueError(f"{path}: not a pcap file ({len(data)} bytes)")
    magic = data[:4]
    little = {b"\xd4\xc3\xb2\xa1": False, b"\x4d\x3c\xb2\xa1": True}
    big = {b"\xa1\xb2\xc3\xd4": False, b"\xa1\xb2\x3c\x4d": True}
    if magic in little:
        end, nano = "<", little[magic]
    elif magic in big:
        end, nano = ">", big[magic]
    else:
        raise ValueError(f"{path}: bad pcap magic {magic.hex()}")
    link = struct.unpack(end + "I", data[20:24])[0]
    pos = 24
    while pos + 16 <= len(data):
        sec, frac, incl, _ = struct.unpack(end + "IIII", data[pos:pos + 16])
        pos += 16
        if pos + incl > len(data):
            break
        yield sec + frac / (1e9 if nano else 1e6), link, data[pos:pos + incl]
        pos += incl


def parse_ip_packet(link, frame):
    """(src_mac, ipv4 packet bytes) or None."""
    if link == 1 and len(frame) >= 14:
        src, etype, off = frame[6:12], struct.unpack("!H", frame[12:14])[0], 14
        if etype == 0x8100 and len(frame) >= 18:
            etype, off = struct.unpack("!H", frame[16:18])[0], 18
    elif link == 113 and len(frame) >= 16:
        addrlen = struct.unpack("!H", frame[4:6])[0]
        src, etype, off = frame[6:6 + min(addrlen, 8)], struct.unpack("!H", frame[14:16])[0], 16
    elif link == 276 and len(frame) >= 20:
        etype, addrlen, off = struct.unpack("!H", frame[0:2])[0], frame[11], 20
        src = frame[12:12 + min(addrlen, 8)]
    else:
        return None
    if etype != 0x0800 or len(frame) < off + 20:
        return None
    return mac_str(src), frame[off:]


def parse_transport(ip):
    """(proto, src, dst, sport, dport, payload) for UDP/TCP over IPv4, else None."""
    ihl = (ip[0] & 0x0F) * 4
    if ip[0] >> 4 != 4 or len(ip) < ihl + 8:
        return None
    proto, total = ip[9], struct.unpack("!H", ip[2:4])[0]
    src, dst = socket.inet_ntoa(ip[12:16]), socket.inet_ntoa(ip[16:20])
    body = ip[ihl:total] if total >= ihl else ip[ihl:]
    if proto == 17:
        if len(body) < 8:  # a bogus IP total length must not raise here
            return None
        sport, dport = struct.unpack("!HH", body[:4])
        return "udp", src, dst, sport, dport, body[8:]
    if proto == 6 and len(body) >= 20:
        sport, dport = struct.unpack("!HH", body[:4])
        return "tcp", src, dst, sport, dport, body[(body[12] >> 4) * 4:]
    return None


# ---- DHCP ------------------------------------------------------------------------------------------------------
def parse_dhcp(payload):
    if len(payload) < 240 or payload[236:240] != b"\x63\x82\x53\x63":
        return None
    op, _, hlen = payload[0], payload[1], payload[2]
    flags = struct.unpack("!H", payload[10:12])[0]
    msg = {"op": op, "xid": payload[4:8].hex(), "broadcast": bool(flags & 0x8000),
           "ciaddr": socket.inet_ntoa(payload[12:16]), "yiaddr": socket.inet_ntoa(payload[16:20]),
           "giaddr": socket.inet_ntoa(payload[24:28]), "chaddr": mac_str(payload[28:28 + min(hlen, 16)]), "opts": {}}
    pos = 240
    while pos < len(payload):
        code = payload[pos]
        if code == 255:
            break
        if code == 0:
            pos += 1
            continue
        if pos + 1 >= len(payload):
            break
        length = payload[pos + 1]
        value = payload[pos + 2:pos + 2 + length]
        msg["opts"][code] = value
        pos += 2 + length
    t = msg["opts"].get(53)
    msg["type"] = DHCP_TYPES.get(t[0], f"type{t[0]}") if t else "?"
    return msg


def ips_of(raw):
    return [socket.inet_ntoa(raw[i:i + 4]) for i in range(0, len(raw) - len(raw) % 4, 4)]


def opt_ip(msg, code):
    return ", ".join(ips_of(msg["opts"][code])) if code in msg["opts"] else ""


# ---- DNS --------------------------------------------------------------------------------------------------------
def read_name(data, pos):
    labels, jumped, end, hops = [], False, pos, 0
    while pos < len(data) and hops < 40:
        size = data[pos]
        if size == 0:
            return ".".join(labels), (pos + 1 if not jumped else end)
        if size & 0xC0 == 0xC0:
            if pos + 1 >= len(data):
                break
            if not jumped:
                end = pos + 2
            pos, jumped, hops = ((size & 0x3F) << 8) | data[pos + 1], True, hops + 1
            continue
        labels.append(data[pos + 1:pos + 1 + size].decode("ascii", "replace").lower())
        pos += 1 + size
    return None, pos


def parse_dns(data):
    """dict(qr, rcode, name, qtype, answers=[ipv4...]) or None."""
    if len(data) < 12:
        return None
    _, flags, qd, an, _, _ = struct.unpack("!6H", data[:12])
    if qd != 1:
        return None
    name, pos = read_name(data, 12)
    if name is None or pos + 4 > len(data):
        return None
    qtype = struct.unpack("!H", data[pos:pos + 2])[0]
    pos += 4
    answers = []
    for _ in range(an):
        _, pos = read_name(data, pos)
        if pos + 10 > len(data):
            break
        rtype, _, _, rdlen = struct.unpack("!HHIH", data[pos:pos + 10])
        rdata = data[pos + 10:pos + 10 + rdlen]
        pos += 10 + rdlen
        if rtype == 1 and rdlen == 4 and len(rdata) == 4:
            answers.append(socket.inet_ntoa(rdata))
    return {"qr": bool(flags & 0x8000), "rcode": flags & 0xF, "name": name, "qtype": QTYPES.get(qtype, str(qtype)),
            "answers": answers}


# ---- report -----------------------------------------------------------------------------------------------------
def collect(path):
    dhcp, dns = [], []
    for ts, link, frame in read_pcap(path):
        parsed = parse_ip_packet(link, frame)
        if not parsed:
            continue
        src_mac, ip = parsed
        t = parse_transport(ip)
        if not t:
            continue
        proto, src, dst, sport, dport, payload = t
        if proto == "udp" and {sport, dport} & {67, 68} and {sport, dport} <= {67, 68}:
            msg = parse_dhcp(payload)
            if msg:
                dhcp.append(dict(msg, ts=ts, src_mac=src_mac, src=src, dst=dst, sport=sport, dport=dport))
        elif 53 in (sport, dport) and SPEAKER_IP in (src, dst):
            if proto == "tcp":
                payload = payload[2:] if len(payload) > 14 else b""
            msg = parse_dns(payload)
            if msg:
                dns.append(dict(msg, ts=ts, src=src, dst=dst, proto=proto))
    return dhcp, dns


def clock(ts):
    return datetime.datetime.fromtimestamp(ts).strftime("%H:%M:%S")


def report(path, name=None):
    dhcp, dns = collect(path)
    out = [f"=== {name or os.path.basename(path)} ==="]
    # 1) who answered
    replies = [m for m in dhcp if m["op"] == 2]
    out.append("-- DHCP servers that sent OFFER/ACK/NAK (sender IP / MAC / server-identifier / DNS handed out):")
    if not replies:
        out.append("   (none seen)")
    groups = collections.defaultdict(lambda: collections.Counter())
    for m in replies:
        key = (label_ip(m["src"]), label_mac(m["src_mac"]), opt_ip(m, 54) or "-", opt_ip(m, 6) or "-")
        groups[key][m["type"]] += 1
    for (ip, mac, sid, dns_opt), counts in sorted(groups.items()):
        out.append(f"   src {ip} mac {mac} server-id {sid} option6 [{dns_opt}]: " +
                   ", ".join(f"{k} x{v}" for k, v in sorted(counts.items())))
    # 2) messages
    out.append("-- DHCP messages (* = client is the speaker):")
    out.append("   time      type      src -> dst                               client                 server-id      requested/yiaddr   opt6 (dns)")
    for m in dhcp:
        star = "*" if m["chaddr"] == SPEAKER_MAC else " "
        addr = opt_ip(m, 50) if m["type"] in ("REQUEST", "DISCOVER") else (m["yiaddr"] if m["yiaddr"] != "0.0.0.0" else m["ciaddr"])
        out.append(f"  {star}{clock(m['ts'])}  {m['type']:<8}  {m['src']}:{m['sport']} -> {m['dst']}:{m['dport']:<5} "
                   f"{m['chaddr']:<17} {opt_ip(m, 54) or '-':<14} {addr or '-':<18} {opt_ip(m, 6) or '-'}"
                   f"{'  [bcast]' if m['broadcast'] else ''}  mac {label_mac(m['src_mac'])}")
    if not dhcp:
        out.append("   (no DHCP packets)")
    # 3) speaker DNS
    asked = collections.defaultdict(collections.Counter)
    answered = collections.defaultdict(lambda: collections.defaultdict(set))
    rcodes = collections.defaultdict(collections.Counter)
    for m in dns:
        if not m["qr"] and m["src"] == SPEAKER_IP:
            asked[m["dst"]][(m["name"], m["qtype"])] += 1
        elif m["qr"] and m["dst"] == SPEAKER_IP:
            answered[m["src"]][m["name"]].update(m["answers"])
            rcodes[m["src"]][m["rcode"]] += 1
    out.append(f"-- DNS queries from the speaker {SPEAKER_IP} by destination server:")
    if not asked:
        out.append("   (none)")
    order = sorted(asked, key=lambda ip: (ip not in (FRONT_IP, BACKEND_IP, GATEWAY_IP), ip))
    if asked:
        out.append("   server                    queries  distinct names")
    for server in order:
        names = asked[server]
        out.append(f"   {label_ip(server):<25} {sum(names.values()):>7}  {len({n for n, _ in names})}")
    for server in order:
        out.append(f"   names asked of {label_ip(server)}:")
        for (qname, qtype), count in sorted(asked[server].items(), key=lambda kv: (-kv[1], kv[0])):
            ans = sorted(answered[server].get(qname, ())) if qtype == "A" else []
            out.append(f"     {count:>4}x {qtype:<5} {qname}" + (f"  -> {', '.join(ans)}" if ans else ""))
    # 4) facts for the hypotheses
    out.append("-- Facts:")
    to_speaker = [m for m in replies if m["chaddr"] == SPEAKER_MAC]
    senders = sorted({f"{m['src']} [{label_mac(m['src_mac'])}]" for m in to_speaker})
    out.append(f"   DHCP replies addressed to the speaker's MAC: {len(to_speaker)}; senders: {', '.join(senders) or 'none seen'}")
    out.append("   option 6 in those replies: " + (", ".join(sorted({opt_ip(m, 6) or '-' for m in to_speaker})) or "none seen"))
    req_ids = sorted({opt_ip(m, 54) for m in dhcp if m["chaddr"] == SPEAKER_MAC and m["type"] == "REQUEST" and opt_ip(m, 54)})
    out.append("   server-identifier in the speaker's REQUESTs (the server it selected): " + (", ".join(req_ids) or "none (INIT-REBOOT/RENEW or no REQUEST seen)"))
    out.append("   speaker DNS went to: " + (", ".join(f"{label_ip(s)} x{sum(asked[s].values())}" for s in order) or "no queries seen"))
    return out


# ---- self-test --------------------------------------------------------------------------------------------------
def _eth_ip_udp(src_mac, dst_mac, src, dst, sport, dport, payload):
    udp = struct.pack("!HHHH", sport, dport, 8 + len(payload), 0) + payload
    ip = struct.pack("!BBHHHBBH4s4s", 0x45, 0, 20 + len(udp), 1, 0, 64, 17, 0, socket.inet_aton(src), socket.inet_aton(dst)) + udp
    return bytes.fromhex(dst_mac.replace(":", "")) + bytes.fromhex(src_mac.replace(":", "")) + b"\x08\x00" + ip


def _dhcp(op, mtype, chaddr, yiaddr="0.0.0.0", opts=()):
    head = struct.pack("!BBBBIHH4s4s4s4s16s", op, 1, 6, 0, 0xDEADBEEF, 0, 0, b"\0" * 4, socket.inet_aton(yiaddr),
                       b"\0" * 4, b"\0" * 4, bytes.fromhex(chaddr.replace(":", "")) + b"\0" * 10)
    body = head + b"\0" * 192 + b"\x63\x82\x53\x63" + bytes([53, 1, mtype])
    for code, value in opts:
        body += bytes([code, len(value)]) + value
    return body + b"\xff"


def _dns(txid, name, answers=()):
    q = b"".join(bytes([len(p)]) + p.encode() for p in name.split(".")) + b"\0" + struct.pack("!HH", 1, 1)
    flags = 0x8180 if answers else 0x0100
    msg = struct.pack("!6H", txid, flags, 1, len(answers), 0, 0) + q
    for addr in answers:
        msg += b"\xc0\x0c" + struct.pack("!HHIH", 1, 1, 5, 4) + socket.inet_aton(addr)
    return msg


def selftest():
    ip = socket.inet_aton
    front_mac, backend_mac, spk = FRONT_MAC, BACKEND_MAC, SPEAKER_MAC
    other_server, other_mac = "192.0.2.7", "02:00:00:00:00:77"
    frames = [
        _eth_ip_udp(spk, "ff:ff:ff:ff:ff:ff", "0.0.0.0", "255.255.255.255", 68, 67,
                    _dhcp(1, 3, spk, opts=[(50, ip(SPEAKER_IP)), (12, b"speaker")])),
        _eth_ip_udp(front_mac, spk, FRONT_IP, SPEAKER_IP, 67, 68,
                    _dhcp(2, 5, spk, SPEAKER_IP, [(54, ip(FRONT_IP)), (6, ip(FRONT_IP)), (51, struct.pack("!I", 3600))])),
        _eth_ip_udp(other_mac, spk, other_server, SPEAKER_IP, 67, 68,
                    _dhcp(2, 5, spk, SPEAKER_IP, [(54, ip(other_server)), (6, ip(BACKEND_IP))])),
        _eth_ip_udp(spk, backend_mac, SPEAKER_IP, BACKEND_IP, 40000, 53, _dns(1, "uniproxy.alice.yandex.net")),
        _eth_ip_udp(backend_mac, spk, BACKEND_IP, SPEAKER_IP, 53, 40000, _dns(1, "uniproxy.alice.yandex.net", [BACKEND_IP])),
        _eth_ip_udp(spk, front_mac, SPEAKER_IP, FRONT_IP, 40001, 53, _dns(2, "quasar.yandex.net")),
        _eth_ip_udp(front_mac, spk, FRONT_IP, SPEAKER_IP, 53, 40001, _dns(2, "quasar.yandex.net", ["203.0.113.230"])),
    ]
    with tempfile.NamedTemporaryFile(suffix=".pcap", delete=False) as handle:
        handle.write(struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, 1))
        for n, frame in enumerate(frames):
            handle.write(struct.pack("<IIII", 1794190000 + n, 0, len(frame), len(frame)) + frame)
        handle.write(struct.pack("<IIII", 1794190099, 0, 500, 500) + b"truncated")  # killed tcpdump: must be ignored
        path = handle.name
    try:
        text = "\n".join(report(path, "selftest"))
    finally:
        os.unlink(path)
    expect = [
        f"src {label_ip(FRONT_IP)} mac {label_mac(front_mac)} server-id {FRONT_IP} option6 [{FRONT_IP}]: ACK x1",
        f"src {other_server} mac {other_mac} server-id {other_server} option6 [{BACKEND_IP}]: ACK x1",
        "REQUEST",
        f"{label_ip(BACKEND_IP)}".ljust(25) + "       1  1",
        f"1x A     uniproxy.alice.yandex.net  -> {BACKEND_IP}",
        "1x A     quasar.yandex.net  -> 203.0.113.230",
        f"senders: {FRONT_IP} [{label_mac(front_mac)}], {other_server} [{other_mac}]",
        f"option 6 in those replies: {FRONT_IP}, {BACKEND_IP}",
        "none (INIT-REBOOT/RENEW or no REQUEST seen)",
        f"speaker DNS went to: {label_ip(FRONT_IP)} x1, {label_ip(BACKEND_IP)} x1",
    ]
    missing = [e for e in expect if e not in text]
    print(text)
    if missing:
        print("\nSELFTEST FAILED, missing:\n  " + "\n  ".join(missing), file=sys.stderr)
        return 1
    print("\nSELFTEST OK")
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("pcap", nargs="*")
    parser.add_argument("--selftest", action="store_true")
    args = parser.parse_args()
    if args.selftest:
        return selftest()
    if not args.pcap:
        parser.error("give at least one pcap file (or --selftest)")
    for path in args.pcap:
        print("\n".join(report(path)))
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())

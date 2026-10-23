#!/usr/bin/env python3
"""LAN DNS relay. Override exact names only for a source IP with its expected ARP MAC.

All other packets pass to explicit upstream IPv4 DNS servers unchanged. No query names,
wire payloads, credentials or HTTP data are logged. Configuration is re-read per request;
invalid/unreadable changes fail closed to forwarding using the last valid configuration.
"""
import argparse
import ipaddress
import json
import re
import socket
import socketserver
import struct
import threading
import time
from pathlib import Path


def validate_config(cfg):
    own=ipaddress.IPv4Address(cfg['bind_ip'])
    ipaddress.IPv4Address(cfg['route_ip'])
    ipaddress.IPv4Address(cfg['target']['ip'])
    if not re.fullmatch(r'(?:[0-9a-f]{2}:){5}[0-9a-f]{2}',cfg['target']['mac'].lower()):
        raise ValueError('Invalid target MAC')
    if not cfg.get('upstreams'):raise ValueError('Explicit upstreams required')
    for raw in cfg['upstreams']:
        addr=ipaddress.IPv4Address(raw)
        if addr==own or addr.is_loopback or addr.is_unspecified or addr.is_multicast:
            raise ValueError('Upstream DNS may not be own address, loopback or invalid')
    if not 1<=int(cfg.get('port',53))<=65535:raise ValueError('Invalid port')
    if not 0<=int(cfg.get('ttl',5))<=30:raise ValueError('Override TTL must be at most 30 seconds')
    if not isinstance(cfg.get('enabled',False),bool):raise ValueError('enabled must be boolean')
    seen=set()
    for target in cfg.get('voice_targets',[cfg['target']]):
        ipaddress.IPv4Address(target['ip'])
        if target['ip'] in seen:raise ValueError('Duplicate voice target IP')
        seen.add(target['ip'])
        if not re.fullmatch(r'(?:[0-9a-f]{2}:){5}[0-9a-f]{2}',target['mac'].lower()):raise ValueError('Invalid voice target MAC')
    for name in cfg.get('domains',[]):
        if not re.fullmatch(r'[a-zA-Z0-9](?:[a-zA-Z0-9.-]{0,251}[a-zA-Z0-9])?',name):
            raise ValueError('Only exact DNS names supported')
    return cfg


def parse_question(packet):
    if len(packet)<12:return None
    _,flags,qdcount,_,_,_=struct.unpack('!6H',packet[:12])
    if flags&0x8000 or flags&0x7800 or qdcount!=1:return None
    pos=12;labels=[]
    while pos<len(packet):
        size=packet[pos];pos+=1
        # Compressed/unusual questions are forwarded intact rather than re-encoded.
        if size&0xc0 or size>63:return None
        if size==0:break
        if pos+size>len(packet):return None
        try:labels.append(packet[pos:pos+size].decode('ascii').lower())
        except UnicodeDecodeError:return None
        pos+=size
        if pos>267:return None
    else:return None
    if pos+4>len(packet):return None
    qtype,qclass=struct.unpack('!HH',packet[pos:pos+4])
    return '.'.join(labels),qtype,qclass,pos+4,flags


def selected_targets(cfg):
    return cfg.get('voice_targets',[cfg['target']]) if cfg.get('mode')=='voice' else [cfg['target']]


def mac_matches(client_ip,cfg,expected_mac=None):
    try:
        for line in Path(cfg.get('arp_file','/proc/net/arp')).read_text().splitlines():
            cols=line.split()
            if len(cols)>=6 and cols[0]==client_ip:
                return int(cols[2],16)&2!=0 and cols[3].lower()==(expected_mac or cfg['target']['mac']).lower()
    except (OSError,ValueError):pass
    return False


def override_response(packet,client_ip,cfg):
    if not cfg.get('enabled',False):return None
    target=next((t for t in selected_targets(cfg) if t['ip']==client_ip),None)
    if target is None or not mac_matches(client_ip,cfg,target['mac']):return None
    question=parse_question(packet)
    if question is None:return None
    name,qtype,qclass,end,flags=question
    if qclass!=1 or name not in {n.lower().rstrip('.') for n in cfg.get('domains',[])}:return None
    # AAAA and HTTPS/SVCB return NODATA to avoid bypassing the scoped A record.
    if qtype not in (1,28,64,65):return None
    count=1 if qtype==1 else 0
    # Preserve RD/CD; synthesized records are deliberately not marked DNSSEC-authenticated.
    response_flags=0x8080|(flags&0x0110)
    header=packet[:2]+struct.pack('!HHHHH',response_flags,1,count,0,0)
    answer=b''
    if count:
        answer=b'\xc0\x0c'+struct.pack('!HHIH',1,1,int(cfg.get('ttl',5)),4)+socket.inet_aton(cfg['route_ip'])
    return header+packet[12:end]+answer


def recv_exact(sock,size):
    data=bytearray()
    while len(data)<size:
        part=sock.recv(size-len(data))
        if not part:raise ConnectionError('Truncated TCP DNS packet')
        data.extend(part)
    return bytes(data)


def forward_udp(packet,upstream,port=53,timeout=1.5):
    with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as sock:
        sock.settimeout(timeout)
        sock.connect((upstream,port))
        sock.send(packet)
        result=sock.recv(65535)
    if len(result)<12 or result[:2]!=packet[:2] or not result[2]&0x80:
        raise ValueError('Invalid upstream DNS response')
    # Retry truncated UDP replies over TCP against the same upstream.
    if result[2]&2:return forward_tcp(packet,upstream,port,timeout)
    return result


def forward_tcp(packet,upstream,port=53,timeout=1.5):
    with socket.create_connection((upstream,port),timeout=timeout) as sock:
        sock.settimeout(timeout)
        sock.sendall(struct.pack('!H',len(packet))+packet)
        size=struct.unpack('!H',recv_exact(sock,2))[0]
        result=recv_exact(sock,size)
    if len(result)<12 or result[:2]!=packet[:2] or not result[2]&0x80:
        raise ValueError('Invalid upstream DNS response')
    return result


def failure_response(packet):
    question=parse_question(packet)
    if question is None:return None
    _,_,_,end,flags=question
    return packet[:2]+struct.pack('!HHHHH',0x8082|(flags&0x0110),1,0,0,0)+packet[12:end]


class Relay:
    def __init__(self,path):
        self.path=Path(path)
        self.lock=threading.Lock()
        self.cfg=validate_config(json.loads(self.path.read_text()))
        self.counts={'forwarded':0,'overridden':0,'failed':0,'config_invalid':0}
        self.workers=threading.BoundedSemaphore(64)
    def config(self):
        try:
            cfg=validate_config(json.loads(self.path.read_text()))
            with self.lock:self.cfg=cfg
            return cfg
        except (OSError,ValueError,KeyError,TypeError):
            with self.lock:
                self.counts['config_invalid']+=1
                cfg=dict(self.cfg)
            cfg['enabled']=False
            return cfg
    def resolve(self,packet,source,tcp=False):
        cfg=self.config()
        if len(packet)<12 or not ipaddress.ip_address(source).is_private:return None
        result=override_response(packet,source,cfg)
        if result is not None:
            with self.lock:self.counts['overridden']+=1
            return result
        forward=forward_tcp if tcp else forward_udp
        for upstream in cfg['upstreams']:
            try:
                result=forward(packet,upstream,int(cfg.get('upstream_port',53)),float(cfg.get('timeout',1.5)))
                with self.lock:self.counts['forwarded']+=1
                return result
            except (OSError,ValueError,ConnectionError):continue
        with self.lock:self.counts['failed']+=1
        return failure_response(packet)


class DNSUDP(socketserver.BaseRequestHandler):
    def handle(self):
        relay=self.server.relay
        if not relay.workers.acquire(False):return
        try:
            packet,sock=self.request
            result=relay.resolve(packet,self.client_address[0])
            if result is not None:sock.sendto(result,self.client_address)
        except (OSError,ValueError):pass
        finally:relay.workers.release()


class DNSTCP(socketserver.BaseRequestHandler):
    def handle(self):
        relay=self.server.relay
        if not relay.workers.acquire(False):return
        try:
            self.request.settimeout(6)
            # Support multiple DNS messages per TCP connection, bounded to prevent starvation.
            for _ in range(20):
                size=struct.unpack('!H',recv_exact(self.request,2))[0]
                if size<12:return
                packet=recv_exact(self.request,size)
                result=relay.resolve(packet,self.client_address[0],tcp=True)
                if result is None:return
                self.request.sendall(struct.pack('!H',len(result))+result)
        except (OSError,ValueError,ConnectionError):pass
        finally:relay.workers.release()


class UDPServer(socketserver.ThreadingUDPServer):
    daemon_threads=True
class TCPServer(socketserver.ThreadingTCPServer):
    daemon_threads=True
    allow_reuse_address=True


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--config',default='/config/config.json')
    args=parser.parse_args()
    relay=Relay(args.config)
    bind=(relay.cfg['bind_ip'],int(relay.cfg.get('port',53)))
    with UDPServer(bind,DNSUDP) as udp,TCPServer(bind,DNSTCP) as tcp:
        udp.relay=tcp.relay=relay
        for server in [udp,tcp]:threading.Thread(target=server.serve_forever,daemon=True).start()
        print(json.dumps({'event':'dns_ready','bind_ip':bind[0],'port':bind[1],'override_enabled':relay.cfg.get('enabled',False)}),flush=True)
        while True:
            time.sleep(30)
            with relay.lock:counts=dict(relay.counts)
            print(json.dumps({'event':'dns_counts',**counts}),flush=True)

if __name__=='__main__':main()

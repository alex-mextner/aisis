#!/usr/bin/env python3
"""Run from Odroid to prove LAN UDP/TCP DNS forwarding, without changing client DNS."""
import argparse
import ipaddress
import json
import socket
import struct
from dns_forwarder import forward_tcp,forward_udp,parse_question

def query(name,qtype=1):
    labels=b''.join(bytes([len(x)])+x.encode('ascii') for x in name.split('.'))+b'\0'
    return struct.pack('!HHHHHH',50321,0x100,1,0,0,0)+labels+struct.pack('!HH',qtype,1)

def skip_name(data,pos):
    for _ in range(128):
        if pos>=len(data):raise ValueError('Truncated name')
        n=data[pos];pos+=1
        if n&0xc0==0xc0:return pos+1
        if n==0:return pos
        if n>63:raise ValueError('Bad name')
        pos+=n
    raise ValueError('Long name')

def a_records(data):
    pos=12
    _,_,qd,an,_,_=struct.unpack('!6H',data[:12])
    for _ in range(qd):pos=skip_name(data,pos)+4
    result=[]
    for _ in range(an):
        pos=skip_name(data,pos)
        typ,cls,ttl,size=struct.unpack('!HHIH',data[pos:pos+10]);pos+=10
        if typ==1 and cls==1 and size==4:result.append(socket.inet_ntoa(data[pos:pos+4]))
        pos+=size
    return sorted(set(result))

def main():
    p=argparse.ArgumentParser()
    p.add_argument('--server',required=True)
    p.add_argument('--upstream',required=True)
    p.add_argument('--port',type=int,default=53)
    args=p.parse_args()
    for name in ['example.org','quasar.yandex.net']:
        packet=query(name)
        original=forward_udp(packet,args.upstream,53,3)
        expected=a_records(original)
        if not expected:raise SystemExit('Upstream returned no A records')
        for protocol,forward in [('udp',forward_udp),('tcp',forward_tcp)]:
            reply=forward(packet,args.server,args.port,3)
            actual=a_records(reply)
            if not actual:raise SystemExit('Relay returned no A records')
            # CDN answer sets may vary; any public response proves no LAN destination override.
            if any(ipaddress.ip_address(x).is_private for x in actual):raise SystemExit('Unexpected private DNS answer on normal client')
            print(json.dumps({'name':name,'protocol':protocol,'rcode':reply[3]&15,'public_addresses':actual,'upstream_public_addresses':expected,'pass':True}),flush=True)
if __name__=='__main__':main()

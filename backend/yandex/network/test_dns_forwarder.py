import importlib.util
import ipaddress
import socket
import socketserver
import struct
import sys
import tempfile
import threading
import unittest
from pathlib import Path

HERE = Path(__file__).parent
spec = importlib.util.spec_from_file_location('dns_forwarder', HERE / 'dns_forwarder.py')
mod = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = mod
if (HERE / 'dns_forwarder.py').exists():
    spec.loader.exec_module(mod)


def query(name='quasar.yandex.net', qtype=1, qid=314):
    labels=b''.join(bytes([len(p)])+p.encode() for p in name.split('.'))+b'\0'
    return struct.pack('!HHHHHH',qid,0x100,1,0,0,0)+labels+struct.pack('!HH',qtype,1)


class ScopeTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(hasattr(mod,"override_response"),"DNS scoping behavior not implemented")
        self.tmp=tempfile.TemporaryDirectory()
        self.arp=Path(self.tmp.name)/'arp'
        self.arp.write_text('IP address HW type Flags HW address Mask Device\n192.0.2.44 0x1 0x2 02:00:00:00:00:44 * eth0\n')
        self.cfg={'enabled':True,'target':{'ip':'192.0.2.44','mac':'02:00:00:00:00:44'},'route_ip':'192.0.2.56','domains':['quasar.yandex.net'],'ttl':5,'arp_file':str(self.arp),'bind_ip':'192.0.2.56','upstreams':['198.51.100.30']}
    def tearDown(self):self.tmp.cleanup()
    def test_scoped_a_uses_configured_destination(self):
        response=mod.override_response(query(),'192.0.2.44',self.cfg)
        self.assertEqual(response[-4:],socket.inet_aton('192.0.2.56'))
        self.assertEqual(struct.unpack('!H',response[6:8])[0],1)
    def test_disabled_or_wrong_client_never_overrides(self):
        self.assertIsNone(mod.override_response(query(),'192.0.2.15',self.cfg))
        self.cfg['enabled']=False
        self.assertIsNone(mod.override_response(query(),'192.0.2.44',self.cfg))
    def test_changed_or_missing_mac_never_overrides(self):
        self.arp.write_text('192.0.2.44 0x1 0x2 02:00:00:00:00:99 * eth0\n')
        self.assertIsNone(mod.override_response(query(),'192.0.2.44',self.cfg))
        self.arp.unlink()
        self.assertIsNone(mod.override_response(query(),'192.0.2.44',self.cfg))
    def test_unrelated_and_subdomain_names_are_unchanged(self):
        for name in ['example.org','sub.quasar.yandex.net']:
            self.assertIsNone(mod.override_response(query(name),'192.0.2.44',self.cfg))
    def test_scoped_aaaa_is_nodata_but_other_client_is_unchanged(self):
        response=mod.override_response(query(qtype=28),'192.0.2.44',self.cfg)
        self.assertEqual(struct.unpack('!H',response[6:8])[0],0)
        self.assertEqual(struct.unpack('!H',response[2:4])[0]&15,0)
        self.assertIsNone(mod.override_response(query(qtype=28),'192.0.2.15',self.cfg))
    def test_malformed_and_compressed_questions_do_not_synthesize(self):
        for payload in [b'abc',struct.pack('!HHHHHH',314,0x100,1,0,0,0)+b'\xc0\x0c\x00\x01\x00\x01']:
            self.assertIsNone(mod.override_response(payload,'192.0.2.44',self.cfg))
    def test_voice_mode_routes_second_owned_target_only_with_matching_mac(self):
        self.cfg['mode']='voice'
        self.cfg['voice_targets']=[self.cfg['target'],{'ip':'192.0.2.15','mac':'02:00:00:00:00:15'}]
        self.arp.write_text(self.arp.read_text()+'192.0.2.15 0x1 0x2 02:00:00:00:00:15 * eth0\n')
        self.assertIsNotNone(mod.override_response(query(),'192.0.2.15',self.cfg))
        self.assertIsNone(mod.override_response(query(),'192.0.2.14',self.cfg))
        self.arp.write_text('192.0.2.15 0x1 0x2 02:00:00:00:00:99 * eth0\n')
        self.assertIsNone(mod.override_response(query(),'192.0.2.15',self.cfg))
    def test_compatibility_stays_single_target_even_with_voice_targets(self):
        self.cfg['mode']='compat'
        self.cfg['voice_targets']=[self.cfg['target'],{'ip':'192.0.2.15','mac':'02:00:00:00:00:15'}]
        self.arp.write_text(self.arp.read_text()+'192.0.2.15 0x1 0x2 02:00:00:00:00:15 * eth0\n')
        self.assertIsNone(mod.override_response(query(),'192.0.2.15',self.cfg))
        self.assertIsNotNone(mod.override_response(query(),'192.0.2.44',self.cfg))
    def test_upstream_may_not_be_loopback_or_own_address(self):
        for upstream in ['192.0.2.56','127.0.0.53']:
            bad=dict(self.cfg,upstreams=[upstream])
            with self.assertRaises(ValueError):mod.validate_config(bad)


class ForwardingTests(unittest.TestCase):
    def test_udp_and_tcp_forward_wire_response_unchanged(self):
        self.assertTrue(hasattr(mod,'forward_udp'),'DNS forwarding behavior not implemented')
        try:
            can_socket=socket.socket();can_socket.close()
        except PermissionError:
            self.skipTest('Local sandbox disallows sockets; run this test on authorized Ubuntu host')
        payload=query('example.org')
        response=payload[:2]+b'\x81\x80'+payload[4:]
        class UDP(socketserver.BaseRequestHandler):
            def handle(self):self.request[1].sendto(response,self.client_address)
        class TCP(socketserver.BaseRequestHandler):
            def handle(self):
                size=struct.unpack('!H',mod.recv_exact(self.request,2))[0]
                self.server.seen=mod.recv_exact(self.request,size)
                data=struct.pack('!H',len(response))+response
                for part in [data[:1],data[1:4],data[4:]]:self.request.sendall(part)
        with socketserver.ThreadingUDPServer(('127.0.0.1',0),UDP) as udp, socketserver.ThreadingTCPServer(('127.0.0.1',0),TCP) as tcp:
            for server in [udp,tcp]:threading.Thread(target=server.serve_forever,daemon=True).start()
            try:
                self.assertEqual(mod.forward_udp(payload,'127.0.0.1',udp.server_address[1],1),response)
                self.assertEqual(mod.forward_tcp(payload,'127.0.0.1',tcp.server_address[1],1),response)
                self.assertEqual(tcp.seen,payload)
            finally:udp.shutdown();tcp.shutdown()

if __name__=='__main__':unittest.main()

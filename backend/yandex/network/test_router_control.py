import importlib.util
import unittest
from unittest.mock import patch
import subprocess
import contextlib
import io
import shlex
from pathlib import Path
spec=importlib.util.spec_from_file_location('router_control',Path(__file__).parent/'router_control.py')
mod=importlib.util.module_from_spec(spec)
if Path(spec.origin).exists():spec.loader.exec_module(mod)

class RouterSafetyTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(hasattr(mod,'validate_snapshot'),'Router safety behavior not implemented')
        self.cfg={'router':{'host':'192.0.2.1'},'bind_ip':'192.0.2.56'}
    def snapshot(self):return {'host':'192.0.2.1','dns':{'dns1':'198.51.100.30','dns2':'198.51.100.40','dns3':'198.51.100.50'},'lease_seconds':3600,'bind_ip':'192.0.2.56'}
    def test_rollback_preserves_all_three_original_dns_addresses(self):
        snap=self.snapshot();mod.validate_snapshot(snap,self.cfg)
        self.assertEqual(mod.dns_changes(snap,rollback=True),snap['dns'])
    def test_restore_does_not_overwrite_someone_elses_later_dns_change(self):
        snap=self.snapshot()
        self.assertTrue(mod.restoration_allowed({'dns1':'192.0.2.56','dns2':'192.0.2.56','dns3':'192.0.2.56'},snap))
        self.assertFalse(mod.restoration_allowed({'dns1':'1.1.1.1','dns2':'8.8.8.8','dns3':'0.0.0.0'},snap))
    def test_forwarding_retention_is_at_least_the_full_dhcp_lease_plus_margin(self):
        self.assertGreaterEqual(mod.retention_seconds(self.snapshot()),3900)
        snap=self.snapshot();snap['lease_seconds']=7200
        self.assertGreaterEqual(mod.retention_seconds(snap),7500)
    def test_voice_mode_only_selects_uniproxy_and_preserves_account_hosts(self):
        self.assertTrue(hasattr(mod,'mode_domains'),'Voice-only selection not implemented')
        cfg={'compat_domains':['quasar.yandex.net','quasar.yandex.ru','scbh.yandex.net'],'voice_domains':['uniproxy.alice.yandex.net']}
        self.assertEqual(mod.mode_domains(cfg,'voice'),['uniproxy.alice.yandex.net'])
        self.assertEqual(mod.mode_domains(cfg,'compat'),cfg['compat_domains'])
    def test_rollback_stops_only_tls_gateways_never_dns_forwarder(self):
        self.assertTrue(hasattr(mod,'gateway_stop_command'),'TLS rollback not implemented')
        cmd=mod.gateway_stop_command({'network_compose':'/private/compose.network.yaml'})
        self.assertEqual(cmd[-2:],['tls-voice','tls-compat'])
        self.assertNotIn('dns-relay',cmd)
        self.assertNotIn('down',cmd)
    def test_ssh_failure_never_reports_gateway_shutdown_or_falls_back_to_direct_docker(self):
        cfg={'network_compose':'/private/compose.network.yaml','bind_ip':'192.0.2.56'}
        with patch.object(mod.subprocess,'run',return_value=subprocess.CompletedProcess([],255,'','SSH rejected')) as run, contextlib.redirect_stdout(io.StringIO()):
            self.assertFalse(mod.stop_gateways(cfg,'/private/config.json'))
        self.assertEqual(run.call_count,1)
        command=run.call_args.args[0]
        self.assertEqual(command[0],'ssh')
        self.assertIn('BatchMode=yes',command)
        self.assertIn('StrictHostKeyChecking=yes',command)
        self.assertIn('ConnectTimeout=5',command)
        remote=shlex.split(command[-1])
        self.assertEqual(remote[-2:],['tls-voice','tls-compat'])
        self.assertNotIn('dns-relay',remote)
    def test_ssh_timeout_returns_failure_without_running_docker_locally(self):
        cfg={'network_compose':'/private/compose.network.yaml','bind_ip':'192.0.2.56'}
        with patch.object(mod.subprocess,'run',side_effect=subprocess.TimeoutExpired('ssh',20)) as run, contextlib.redirect_stdout(io.StringIO()):
            self.assertFalse(mod.stop_gateways(cfg,'/private/config.json'))
        self.assertEqual(run.call_count,1)
    def test_snapshot_cannot_select_unexpected_gateway_or_bind_address(self):
        for key,value in [('host','192.0.2.2'),('bind_ip','192.0.2.1')]:
            snap=self.snapshot();snap[key]=value
            with self.assertRaises(ValueError):mod.validate_snapshot(snap,self.cfg)
if __name__=='__main__':unittest.main()

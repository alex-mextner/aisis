import importlib.util
import unittest
from unittest.mock import patch
import subprocess
import contextlib
import io
import json
import shlex
import sys
import tempfile
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


class LegacyTopologyTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'config.json'
        self.cfg=json.loads(Path(__file__).with_name('config.example.json').read_text())
        self.cfg.update(route_ip='192.0.2.57',enabled=True,network_compose='/private/compose.network.yaml')
        self.path.write_text(json.dumps(self.cfg))
    def test_asymmetric_config_can_disable_overrides_and_restore_original_dns(self):
        snapshot=Path(self.tmp.name)/'router-before.json'
        original={'dns1':'198.51.100.30','dns2':'198.51.100.40','dns3':'198.51.100.50'}
        snapshot.write_text(json.dumps({'host':'192.0.2.1','bind_ip':'192.0.2.56','dns':original,'lease_seconds':3600}))
        before=snapshot.read_bytes()
        current=dict.fromkeys(original,'192.0.2.56')
        class Driver:
            def read_area(self,area):return dict(current)
            def plan_area(self,area,changes):return changes
        class CLI:
            def run_plan(self,driver,plan,args):
                if not args.dry_run:current.update(plan)
        argv=['router_control.py','rollback','--apply','--config',str(self.path),'--snapshot',str(snapshot)]
        with patch.object(sys,'argv',argv), patch.object(mod,'router_session',return_value=contextlib.nullcontext((Driver(),CLI()))), patch.object(mod,'stop_gateways',return_value=True), patch.object(mod.signal,'signal'), patch.object(mod.signal,'alarm'), contextlib.redirect_stdout(io.StringIO()):
            try:mod.main()
            except ValueError as exc:self.fail(f'Legacy topology must remain eligible for rollback: {exc}')
        self.assertFalse(json.loads(self.path.read_text())['enabled'])
        self.assertEqual(current,original)
        self.assertEqual(snapshot.read_bytes(),before)
        self.assertGreaterEqual(json.loads(snapshot.with_name('forwarding-retention.json').read_text())['seconds'],3900)
    def test_asymmetric_config_refuses_compat_and_voice_without_writing(self):
        before=self.path.read_bytes()
        for selected in ['compat','voice']:
            with self.subTest(mode=selected), self.assertRaises(ValueError):mod.mode(self.path,True,selected)
            self.assertEqual(self.path.read_bytes(),before)
    def test_asymmetric_config_refuses_preparation_before_writing_files(self):
        before=self.path.read_bytes()
        result=subprocess.run([sys.executable,str(Path(__file__).with_name('prepare_local.py')),'--config',str(self.path)],capture_output=True,text=True)
        self.assertNotEqual(result.returncode,0)
        self.assertEqual(self.path.read_bytes(),before)
        self.assertFalse(self.path.with_name('compat-nginx.conf').exists())
        self.assertFalse(self.path.with_name('voice-nginx.conf').exists())
if __name__=='__main__':unittest.main()

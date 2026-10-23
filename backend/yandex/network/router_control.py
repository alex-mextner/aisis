#!/usr/bin/env python3
"""Guarded router-cli plans and rollback. Defaults to DRY RUN; never displays credentials.

Only explicit --apply executes a reservation/DNS plan. apply-dns additionally requires
an already active independent rollback timer, unchanged baseline DNS and stable server
reservation. The inventory lock is held until the global admin session has closed.
"""
import argparse
import contextlib
import datetime
import fcntl
import ipaddress
import json
import os
import signal
import shlex
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path



def validate_snapshot(snap,cfg):
    ipaddress.IPv4Address(snap['host']);ipaddress.IPv4Address(snap['bind_ip'])
    if snap['host']==snap['bind_ip']:raise ValueError('Router and DNS relay must differ')
    if snap['host']!=cfg['router']['host'] or snap['bind_ip']!=cfg['bind_ip']:raise ValueError('Unexpected router/server')
    if set(snap.get('dns',{}))!={'dns1','dns2','dns3'}:raise ValueError('All original DNS fields required')
    for value in snap['dns'].values():ipaddress.IPv4Address(value)
    lease=int(snap.get('lease_seconds',0))
    if lease<=0 or lease>86400*30:raise ValueError('Invalid DHCP lease')
    return snap


def dns_changes(snap,rollback=False):
    return dict(snap['dns']) if rollback else {k:snap['bind_ip'] for k in ['dns1','dns2','dns3']}


def restoration_allowed(current,snap):
    fields={k:current.get(k) for k in ['dns1','dns2','dns3']}
    return fields in [dns_changes(snap),dns_changes(snap,rollback=True)]


def retention_seconds(snap):return max(3600,int(snap['lease_seconds']))+300


def write_json(path,data,overwrite=True):
    path=Path(path)
    path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    if path.exists() and not overwrite:raise FileExistsError('Preserve existing original snapshot')
    fd,tmp=tempfile.mkstemp(prefix=path.name+'.',dir=path.parent)
    try:
        with os.fdopen(fd,'w') as out:
            json.dump(data,out,indent=2);out.write('\n');out.flush();os.fsync(out.fileno())
        os.replace(tmp,path)
    finally:
        if os.path.exists(tmp):os.unlink(tmp)


def mode_domains(cfg,selected):
    if selected=='voice':return list(cfg.get('voice_domains',['uniproxy.alice.yandex.net']))
    if selected=='compat':return list(cfg.get('compat_domains',['quasar.yandex.net','quasar.yandex.ru','scbh.yandex.net']))
    return list(cfg.get('domains',[]))


def mode(path,enabled,selected='forwarding'):
    from dns_forwarder import validate_config
    cfg=validate_config(json.loads(Path(path).read_text()))
    cfg['enabled']=enabled
    cfg['domains']=mode_domains(cfg,selected)
    cfg['mode']=selected
    validate_config(cfg)
    write_json(path,cfg)
    print(json.dumps({'event':'dns_mode','override_enabled':enabled,'mode':selected}),flush=True)


@contextlib.contextmanager
def router_session(cfg,source=None):
    host=cfg['router']['host']
    sys.path.insert(0,str(Path(source or cfg['router']['source_path']).expanduser()))
    from router_cli import credentials
    from router_cli.commands import _common as C
    from router_cli.config import db_path
    lockpath=db_path().parent/'inventory-update.lock'
    # Existing lock only: no new lock namespace and no creation of router runtime files.
    with lockpath.open('rb') as lock:
        fcntl.flock(lock.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        os.environ.pop('ROUTER_CLI_KEEP_SESSION',None)
        driver=None
        try:
            profile=credentials.entry(host)
            if profile is None or profile.driver not in ('','ubee_evw32c'):raise ValueError('Existing Ubee profile required')
            args=argparse.Namespace(host=host,driver='ubee_evw32c',user=None,timeout=3,keep_session=False)
            driver=C.open_driver(args)
            yield driver,C
        finally:
            signal.alarm(0)
            opened=bool(driver and getattr(driver,'_opened_session',False))
            closed=C.end_sessions()
            print(json.dumps({'event':'router_session_cleanup','opened_by_tool':opened,'logout_reported_success':host in closed}),flush=True)
            fcntl.flock(lock.fileno(),fcntl.LOCK_UN)


def run_plan(driver,C,plan,apply):
    args=argparse.Namespace(dry_run=not apply,yes=False,json=True,show_secrets=False)
    return C.run_plan(driver,plan,args)


def gateway_stop_command(cfg):
    compose=Path(cfg['network_compose']).expanduser()
    if not compose.is_absolute() or compose.name!='compose.network.yaml':raise ValueError('Private absolute network Compose path required')
    return ['docker','compose','-f',str(compose),'stop','--timeout','5','tls-voice','tls-compat']


def gateway_ssh_command(cfg,config_path):
    environment=['env','AISIS_NETWORK_CONFIG_DIR='+str(Path(config_path).expanduser().resolve().parent),'AISIS_SPEAKER_BIND_IP='+cfg['bind_ip']]
    remote=shlex.join(environment+gateway_stop_command(cfg))
    return ['ssh','-o','BatchMode=yes','-o','StrictHostKeyChecking=yes','-o','ConnectTimeout=5','localhost',remote]


def stop_gateways(cfg,config_path):
    # A fresh SSH login obtains current supplemental groups; the user manager can have
    # stale groups and must not run Docker directly. All env arguments here are nonsecret.
    command=gateway_ssh_command(cfg,config_path)
    try:
        result=subprocess.run(command,capture_output=True,text=True,timeout=20)
        ok=result.returncode==0
        detail={'returncode':result.returncode}
    except (OSError,subprocess.TimeoutExpired) as error:
        ok=False;detail={'error_type':type(error).__name__}
    print(json.dumps({'event':'tls_gateways_stopped','success':ok,**detail,'dns_forwarder_kept_running':True}),flush=True)
    return ok


def check_forwarding(bind):
    from dns_forwarder import forward_udp,forward_tcp
    from probe_dns import query,a_records
    for method in [forward_udp,forward_tcp]:
        result=method(query('example.org'),bind,53,3)
        records=a_records(result)
        if not records or any(ipaddress.ip_address(x).is_private for x in records):raise ValueError('LAN DNS forwarding not healthy')
    with socket.create_connection((bind,443),timeout=3):pass


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('action',choices=['prepare','reserve','apply-dns','rollback','forwarding','compat','voice'])
    p.add_argument('--snapshot',default=str(Path.home()/'.local/state/aisis-yandex-network/router-before.json'))
    p.add_argument('--config',default=str(Path.home()/'.config/aisis-yandex/network/config.json'))
    p.add_argument('--router-source',help='override installed router-cli source from private config')
    p.add_argument('--rollback-timer',default='aisis-network-rollback.timer')
    p.add_argument('--apply',action='store_true',help='explicitly execute; omitted means dry run')
    args=p.parse_args()
    from dns_forwarder import validate_config
    cfg=validate_config(json.loads(Path(args.config).read_text()))
    host=cfg['router']['host'];bind=cfg['bind_ip'];server_mac=cfg['router']['server_mac'].lower()
    if args.action in ('forwarding','compat','voice'):
        if args.apply:mode(args.config,args.action!='forwarding',args.action)
        else:print(json.dumps({'dry_run':True,'config':args.config,'enabled':args.action!='forwarding','mode':args.action}))
        return
    # Keep a session bounded; finally still receives its own logout timeout.
    def deadline(*_):raise TimeoutError('Router operation deadline')
    signal.signal(signal.SIGALRM,deadline)
    signal.alarm(35)
    try:
        if args.action=='prepare':
            if Path(args.snapshot).exists():raise FileExistsError('Original snapshot already exists; do not overwrite')
            with router_session(cfg,args.router_source) as (driver,C):
                lan=driver.read_area('lan')
                dhcp=driver.read_area('dhcp')
                reservations=driver.reservations()
                snap=validate_snapshot({'host':host,'bind_ip':bind,'dns':{k:lan[k] for k in ['dns1','dns2','dns3']},'lease_seconds':int(dhcp['lease_time']),'saved_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'server_reserved_before':any(r.mac==server_mac and r.ip==bind for r in reservations)},cfg)
                write_json(args.snapshot,snap,overwrite=False)
                print(json.dumps({'event':'snapshot_saved','path':args.snapshot,'snapshot':snap,'reservation_count':len(reservations)}),flush=True)
                # Reviewable plans, never executed during prepare.
                run_plan(driver,C,driver.plan_reserve(server_mac,bind,None),False)
                run_plan(driver,C,driver.plan_area('lan',dns_changes(snap)),False)
            return
        snap=validate_snapshot(json.loads(Path(args.snapshot).read_text()),cfg)
        tls_stop_ok=True
        if args.action=='rollback' and args.apply:
            # Restore normal answers immediately even when the gateway is unreachable.
            mode(args.config,False)
            write_json(Path(args.snapshot).with_name('forwarding-retention.json'),{'do_not_stop_dns_before_unix':time.time()+retention_seconds(snap),'seconds':retention_seconds(snap),'reason':'Clients can retain previously issued DNS DHCP options'})
            try:tls_stop_ok=stop_gateways(cfg,args.config)
            except Exception as exc:
                tls_stop_ok=False
                print(json.dumps({'event':'tls_gateway_stop_error','type':type(exc).__name__}),flush=True)
        if args.action=='apply-dns' and args.apply:
            gateway_stop_command(cfg)
            if not Path(cfg['network_compose']).is_file():raise ValueError('Rollback Compose path missing')
            timer=subprocess.run(['systemctl','--user','is-active',args.rollback_timer],capture_output=True,text=True,timeout=3)
            if timer.returncode!=0 or timer.stdout.strip()!='active':raise ValueError('Independent rollback timer must already be active')
            check_forwarding(bind)
        with router_session(cfg,args.router_source) as (driver,C):
            if args.action=='reserve':
                run_plan(driver,C,driver.plan_reserve(server_mac,bind,None),args.apply)
                return
            current=driver.read_area('lan')
            if args.action=='apply-dns':
                if {k:current.get(k) for k in snap['dns']}!=snap['dns']:raise ValueError('Current DNS no longer matches original snapshot')
                if args.apply and not any(r.mac==server_mac and r.ip==bind for r in driver.reservations()):raise ValueError('Ubuntu DNS server must already have its correct reservation')
                run_plan(driver,C,driver.plan_area('lan',dns_changes(snap)),args.apply)
            elif args.action=='rollback':
                if not restoration_allowed(current,snap):raise ValueError('External DNS change detected; forwarding is safe but automatic overwrite refused')
                run_plan(driver,C,driver.plan_area('lan',dns_changes(snap,rollback=True)),args.apply)
                if args.apply:
                    print(json.dumps({'event':'rollback_applied','keep_dns_forwarding_seconds':retention_seconds(snap),'existing_tls_connections_closed':tls_stop_ok}),flush=True)
                    if not tls_stop_ok:raise RuntimeError('TLS gateway shutdown needs review')
    finally:signal.alarm(0)

if __name__=='__main__':
    try:main()
    except Exception as exc:
        # Upstream exception bodies can contain form data; do not emit their strings.
        print(json.dumps({'event':'router_control_error','type':type(exc).__name__}),file=sys.stderr)
        raise SystemExit(1)

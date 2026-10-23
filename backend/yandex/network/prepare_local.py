#!/usr/bin/env python3
"""Render private compatibility config; optionally arm independent rollback.

The public repository contains no LAN inventory. Supply a private config.json and retain
its directory outside the checkout. No router login or network setting changes occur here.
"""
import argparse
import ipaddress
import json
import os
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path
from dns_forwarder import validate_config


def render(cfg,mode='compat'):
    validate_config(cfg,for_override=True)
    for ip in cfg.get('admin_probe_ips',[]):ipaddress.IPv4Address(ip)
    source=Path(__file__).with_name('compat-nginx.conf.template').read_text()
    targets=cfg.get('voice_targets',[cfg['target']]) if mode=='voice' else [cfg['target']]
    allow='\n'.join('    allow '+ip+';' for ip in dict.fromkeys(t['ip'] for t in targets))
    return source.replace('@@ORIGINAL_DNS@@',' '.join(cfg['upstreams'])).replace('@@TARGET_ALLOW_RULES@@',allow).replace('@@ADMIN_ALLOW_RULES@@','\n'.join('    allow '+ip+';' for ip in cfg.get('admin_probe_ips',[])))


def write_private(path,text):
    fd,tmp=tempfile.mkstemp(prefix=path.name+'.',dir=path.parent)
    try:
        with os.fdopen(fd,'w') as stream:stream.write(text)
        os.replace(tmp,path)
    finally:
        if os.path.exists(tmp):os.unlink(tmp)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--config',required=True)
    p.add_argument('--snapshot')
    p.add_argument('--arm-rollback',type=int,metavar='SECONDS')
    p.add_argument('--apply',action='store_true',help='actually arm timer; without this print its command')
    args=p.parse_args()
    path=Path(args.config).expanduser().resolve()
    cfg=validate_config(json.loads(path.read_text()),for_override=True)
    # Bind the rollback to this deployed network stack, outside the public checkout.
    cfg['network_compose']=str(Path(__file__).resolve().parent.parent/'compose.network.yaml')
    write_private(path,json.dumps(cfg,indent=2)+'\n')
    output=path.with_name('compat-nginx.conf')
    write_private(output,render(cfg,'compat'))
    write_private(path.with_name('voice-nginx.conf'),render(cfg,'voice'))
    print('AISIS_NETWORK_CONFIG_DIR='+str(path.parent))
    print('AISIS_SPEAKER_BIND_IP='+cfg['bind_ip'])
    if args.arm_rollback is not None:
        if not 60<=args.arm_rollback<=900 or not args.snapshot:raise ValueError('Rollback needs snapshot and 60..900 second deadline')
        snapshot=Path(args.snapshot).expanduser().resolve()
        if not snapshot.is_file():raise ValueError('Original snapshot must already exist')
        command=['systemd-run','--user','--collect','--unit=aisis-network-rollback',f'--on-active={args.arm_rollback}s','--timer-property=AccuracySec=1s','--property=Type=oneshot','--property=TimeoutStartSec=45s','--property=Restart=on-failure','--property=RestartSec=90s','--property=StartLimitIntervalSec=600s','--property=StartLimitBurst=4','--',sys.executable,str(Path(__file__).with_name('router_control.py').resolve()),'rollback','--apply','--config',str(path),'--snapshot',str(snapshot)]
        print('ROLLBACK_COMMAND='+shlex.join(command),flush=True)
        if args.apply:
            result=subprocess.run(command,capture_output=True,text=True,timeout=8)
            print(json.dumps({'rollback_timer_armed':result.returncode==0,'returncode':result.returncode}),flush=True)
            if result.returncode:raise SystemExit(result.returncode)
if __name__=='__main__':main()

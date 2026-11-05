#!/usr/bin/env python3
"""Host-side real model/tools/relay preflight with production native launch flags."""
import argparse
import os
from pathlib import Path
import subprocess

import native
import pilot


def check(agent, image, label, ssh_host=None):
    family = native.configure(agent)
    # Preflight never runs a second relay-listening harness for the identity.
    # The temporary container runs only the task adapter plus signed CLI reads.
    pid = int(pilot.systemctl('show', pilot.ORIGINAL_SERVICE, '-p', 'MainPID', '--value').stdout)
    if not pid:
        pid = int(pilot.systemctl('show', pilot.NAME + '.service', '-p', 'MainPID', '--value').stdout)
    env = dict(x.split('=', 1) for x in Path(f'/proc/{pid}/environ').read_text().split('\0') if '=' in x)
    code = '''import os,sys,json,subprocess
sys.path.insert(0,"/opt/buzz")
from relay_tunnel import Tunnel
target=json.loads(os.environ.pop("BUZZ_PILOT_RELAY_TARGET"))
relay_probe=Tunnel(("127.0.0.1",443),(target[0],int(target[1])))
relay_probe.start()
'''
    code += Path(__file__).with_name('acp_probe.py').read_text()
    code += '''
transport=subprocess.run(["buzz","channels","list"],capture_output=True,timeout=30)
assert transport.returncode==0,"native relay read failed"
print("native_authenticated_relay_read=passed",flush=True)
relay_probe.shutdown()
'''
    if ssh_host:
        if ssh_host not in {'ag', 'm2', 'm4'}:
            raise ValueError('Unapproved probe destination')
        code += f'''
remote=subprocess.run(["ssh","-o","ConnectTimeout=10",{ssh_host!r},"printf NATIVE_SSH_READY"],capture_output=True,timeout=20)
assert remote.returncode==0 and remote.stdout==b"NATIVE_SSH_READY","native SSH probe failed"
print("native_ssh=passed",flush=True)
'''
    original_call = subprocess.call

    def probe(args):
        # Use separate container name; production volumes intentionally retained.
        args[args.index('--name') + 1] = pilot.NAME + '-preflight'
        args = args[:-1] + ['--interactive', '--entrypoint', 'python3', image, '-',
                            '--cwd', str(pilot.WORKSPACE_ROOT / agent), '--label', label,
                            '--seconds', '120']
        if env.get('BUZZ_ACP_MODEL'):
            args += ['--model', env['BUZZ_ACP_MODEL']]
        if env.get('BUZZ_ACP_MCP_COMMAND'):
            args += ['--with-mcp']
        args += ['/opt/buzz/bin/native-adapter']
        if family in {'cursor', 'omp'}:
            args += ['acp']
        return subprocess.run(args, input=code, text=True, timeout=240).returncode

    previous = dict(os.environ)
    try:
        os.environ.clear()
        os.environ.update(env)
        pilot.subprocess.call = probe
        return native.start(image)
    finally:
        pilot.subprocess.call = original_call
        os.environ.clear()
        os.environ.update(previous)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--agent', required=True, choices=native.PROFILES)
    parser.add_argument('--image', required=True, type=pilot.image_id)
    parser.add_argument('--label', default='native-check-20261104')
    parser.add_argument('--ssh-host')
    args = parser.parse_args()
    raise SystemExit(check(args.agent, args.image, args.label, args.ssh_host))

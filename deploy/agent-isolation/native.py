#!/usr/bin/env python3
"""Host controller for explicitly selected whole native-agent containers."""
import argparse
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import time
from urllib.parse import urlsplit

import pilot
import workspace
import native_sandbox

CODEX_SANDBOX = False

PROFILES = {
    'claude-haiku': (61040, 'Claude-Haiku', 'claude'),
    'claude-sonnet': (61041, 'Claude-Sonnet', 'claude'),
    'claude-opus': (61042, 'Claude-Opus', 'claude'),
    'codex': (61043, 'Codex', 'codex'),
    'luna': (61044, 'Luna', 'codex'),
    'cursor': (61045, 'Cursor', 'cursor'),
    'fable': (61046, 'Fable', 'cursor'),
    'opus': (61047, 'Opus', 'cursor'),
    'omp': (61048, 'Omp', 'omp'),
    'sol': (61049, 'Sol', 'codex'),
}
FORWARDED = pilot.FORWARDED | {
    'BUZZ_ACP_MODEL', 'BUZZ_ACP_AUTO_REPLY_TEXT', 'CURSOR_API_KEY',
    'CLAUDE_CODE_OAUTH_TOKEN', 'ANTHROPIC_API_KEY', 'OPENAI_API_KEY',
    'CODEX_HOME',
}


def configure(agent, codex_sandbox=False):
    global CODEX_SANDBOX
    if codex_sandbox and agent not in {'codex', 'sol'}:
        raise ValueError('The reviewed sandbox policy is only for Codex and Sol')
    uid, display, family = PROFILES[agent]
    pilot.PROFILES[agent] = (uid, display)
    pilot.configure(agent)
    CODEX_SANDBOX = codex_sandbox
    return family


def runtime_args(image, name=None, entrypoint=None, network='bridge', volume_prefix=None):
    prefix = volume_prefix or pilot.NAME
    args = pilot.runtime_args(image, name=name, volume=prefix + '-workspace', entrypoint=entrypoint, network=network)
    # Docker copy-up can reset an EMPTY volume's root ownership from the
    # image mountpoint. Prepared volumes must never be initialized again.
    for index, argument in enumerate(args):
        if argument.startswith('type=volume,'):
            args[index] = argument + ',volume-nocopy'
    args[args.index('--memory') + 1] = '4g'
    args[args.index('--pids-limit') + 1] = '512'
    args[args.index('--workdir') + 1] = str(pilot.WORKSPACE_ROOT / pilot.AGENT)
    args += ['--mount', f'type=volume,source={prefix}-home,target=/home/paul,volume-nocopy',
             '--mount', f'type=volume,source={prefix}-ssh,target=/home/paul/.ssh,readonly,volume-nocopy',
             '--mount', f'type=volume,source={prefix}-workspace,target={pilot.WORKSPACE_ROOT / pilot.AGENT},volume-nocopy',
             '--env', 'BUZZ_NEST=' + str(pilot.WORKSPACE_ROOT / pilot.AGENT),
             '--env', 'BUZZ_ACP_AGENT_ARGS=' + ('acp' if PROFILES[pilot.AGENT][2] in {'cursor', 'omp'} else '')]
    if pilot.AGENT == 'claude-opus':
        args += ['--mount', f'type=volume,source={prefix}-desktop,target=/home/paul/.config/desktop-mcp,readonly,volume-nocopy']
    if CODEX_SANDBOX:
        for option in native_sandbox.security_options():
            args += ['--security-opt', option]
    return args


def audit(name, prefix=None):
    """Inspect real launch authority without emitting Docker's secret env values."""
    prefix = prefix or pilot.NAME
    data = json.loads(pilot.docker(['inspect', name], capture_output=True, text=True).stdout)[0]
    host = data['HostConfig']
    if CODEX_SANDBOX:
        native_sandbox.audit_options(host['SecurityOpt'], data['AppArmorProfile'])
    elif (host['SecurityOpt'] != ['no-new-privileges:true'] or
          data['AppArmorProfile'] != 'docker-default'):
        raise ValueError('Container security policy does not match the selected native boundary')
    if (data['Config']['User'] != f'{pilot.UID}:{pilot.UID}' or
            host['NetworkMode'] != 'bridge' or host['PidMode'] or
            host['IpcMode'] != 'private' or host['Privileged'] or
            not host['ReadonlyRootfs'] or host['CapAdd'] or
            host['CapDrop'] != ['ALL'] or
            'no-new-privileges:true' not in host['SecurityOpt']):
        raise ValueError('Container launch authority does not match the native boundary')
    expected = {
        ('/workspace', prefix + '-workspace', True),
        (str(pilot.WORKSPACE_ROOT / pilot.AGENT), prefix + '-workspace', True),
        ('/home/paul', prefix + '-home', True),
        ('/home/paul/.ssh', prefix + '-ssh', False),
    }
    if pilot.AGENT == 'claude-opus':
        expected.add(('/home/paul/.config/desktop-mcp', prefix + '-desktop', False))
    actual = {(m['Destination'], m.get('Name'), m['RW']) for m in data['Mounts'] if m['Type'] != 'tmpfs'}
    if actual != expected or any(m['Type'] not in {'volume', 'tmpfs'} for m in data['Mounts']):
        raise ValueError('Unexpected native mount authority')
    print(json.dumps({'native_boundary': 'passed', 'uid': pilot.UID, 'container': name}))


def start(image, volume_prefix=None):
    pilot.image_id(image)
    family = PROFILES[pilot.AGENT][2]
    actual = pilot.docker(['image', 'inspect', image, '--format',
                           '{{index .Config.Labels "buzz.isolation.native"}}'],
                          capture_output=True, text=True).stdout.strip()
    if actual != family:
        raise ValueError('Image runtime does not match the selected agent')
    args = runtime_args(image, volume_prefix=volume_prefix)
    relay = urlsplit(os.environ['BUZZ_RELAY_URL']).hostname
    if not relay:
        raise ValueError('Missing relay hostname')
    address = socket.getaddrinfo(relay, 443, family=socket.AF_INET,
                                 type=socket.SOCK_STREAM)[0][4][0]
    args += ['--add-host', relay + ':127.0.0.1']
    # Docker's resolver cannot use the host's MagicDNS. Preserve the fixed
    # fleet names agents already use, without mounting the host DNS setup.
    for host in ['ag', 'm2', 'm4', 'gx10', 'max', 'm5', 'oracle']:
        for name in [host, host + '.tail0a48eb.ts.net']:
            if name == relay:
                continue
            try:
                resolved = socket.getaddrinfo(name, 22, family=socket.AF_INET,
                                              type=socket.SOCK_STREAM)[0][4][0]
            except socket.gaierror:
                continue  # A name unavailable on the original host stays unavailable.
            args += ['--add-host', name + ':' + resolved]
    for key in sorted(FORWARDED.intersection(os.environ)):
        args += ['--env', key]
    bridge = pilot.docker(['network', 'inspect', 'bridge', '--format',
                           '{{(index .IPAM.Config 0).Gateway}}'],
                          capture_output=True, text=True).stdout.strip()
    with ExitStack() as cleanup:
        tunnel = cleanup.enter_context(pilot.Tunnel((bridge, 0), (address, 443)))
        port = tunnel.start()
        cleanup.callback(tunnel.shutdown)
        args += ['--env', 'BUZZ_PILOT_RELAY_TARGET=' + json.dumps([bridge, port])]
        return subprocess.call(['docker', *args, image])


def activate(image, env_file, snapshot):
    pilot.image_id(image)
    script = Path(__file__).resolve()
    env_file = Path(env_file).resolve(strict=True)
    record = json.loads(Path(snapshot).read_text())
    expected_policy = native_sandbox.verify() if CODEX_SANDBOX else None
    if record.get('sandbox_policy') != expected_policy:
        raise ValueError('Snapshot sandbox policy does not match selected launch')
    if record['agent'] != pilot.AGENT or record['image'] != image:
        raise ValueError('Snapshot belongs to a different agent/image')
    if hashlib.sha256(env_file.read_bytes()).hexdigest() != record['environment_file_sha256']:
        raise ValueError('Agent settings changed after preparation')
    original_pid = int(pilot.systemctl('show', pilot.ORIGINAL_SERVICE, '-p', 'MainPID', '--value').stdout)
    if original_pid != record['original_pid']:
        raise ValueError('Original service changed after preparation')
    source = pilot.WORKSPACE_ROOT / pilot.AGENT
    if workspace.digest(source) != record['workspace']:
        raise ValueError('Workspace changed after preparation; original left running')
    if any(c in str(p) for p in [script, env_file] for c in ' \t\n\r%"\\'):
        raise ValueError('Unsupported unit path')
    expected_uid = f'{pilot.UID}:{pilot.UID}'
    actual_uid = pilot.docker(['image', 'inspect', image, '--format', '{{.Config.User}}'],
                              capture_output=True, text=True).stdout.strip()
    if actual_uid != expected_uid:
        raise ValueError('Image identity mismatch')
    # Both copies must already have passed the migration and auth probes.
    volumes = [pilot.VOLUME, pilot.NAME + '-home', pilot.NAME + '-ssh']
    if pilot.AGENT == 'claude-opus':
        volumes.append(pilot.NAME + '-desktop')
    for volume in volumes:
        pilot.docker(['volume', 'inspect', volume], stdout=subprocess.DEVNULL)
    unit = Path.home() / '.config/systemd/user' / (pilot.NAME + '.service')
    if unit.exists():
        raise ValueError('Native service already exists; inspect it before replacement')
    if pilot.systemctl('show', pilot.ORIGINAL_SERVICE, '-p', 'ActiveState', '--value').stdout.strip() != 'active':
        raise ValueError('Original service is not active')
    declared = {line.split('=', 1)[0] for line in env_file.read_text().splitlines() if '=' in line}
    unset = ' '.join(sorted(FORWARDED - declared))
    unit.write_text(f'''[Unit]
Description=Isolated {pilot.DISPLAY_NAME} native runtime
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
Environment=PATH=/usr/local/bin:/usr/bin:/bin
EnvironmentFile={env_file}
UnsetEnvironment={unset}
ExecStart=/usr/bin/python3 "{script}" --agent {pilot.AGENT} run --image {image}{' --codex-sandbox' if CODEX_SANDBOX else ''}
ExecStop=/usr/bin/docker stop --time 25 {pilot.NAME}
Restart=on-failure
RestartSec=15
TimeoutStopSec=45
UMask=0077
StandardOutput=null
StandardError=journal

[Install]
WantedBy=default.target
''')
    pilot.systemctl('daemon-reload')
    try:
        pilot.systemctl('disable', '--now', pilot.ORIGINAL_SERVICE)
        if pilot.systemctl('show', pilot.ORIGINAL_SERVICE, '-p', 'MainPID', '--value').stdout.strip() != '0':
            raise RuntimeError('Original still has a process')
        if workspace.digest(source) != record['workspace']:
            raise RuntimeError('Workspace changed before old process exited')
        pilot.systemctl('enable', '--now', pilot.NAME + '.service')
        for _ in range(30):
            running = subprocess.run(['docker', 'inspect', '--format', '{{.State.Running}}', pilot.NAME],
                                     capture_output=True, text=True, timeout=10)
            if running.returncode == 0 and running.stdout.strip() == 'true':
                time.sleep(2)
                pilot.systemctl('is-active', pilot.NAME + '.service')
                print('Native container started; original stopped and disabled')
                return
            time.sleep(1)
        raise RuntimeError('Native container did not start')
    except Exception:
        pilot.systemctl('disable', '--now', pilot.NAME + '.service')
        remaining = pilot.docker(['ps', '--filter', 'name=^/' + pilot.NAME + '$', '--format', '{{.ID}}'],
                                  capture_output=True, text=True).stdout.strip()
        if remaining:
            raise RuntimeError('Replacement is still running; original remains stopped')
        pilot.systemctl('enable', '--now', pilot.ORIGINAL_SERVICE)
        unit.unlink()
        pilot.systemctl('daemon-reload')
        raise


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--agent', required=True, choices=PROFILES)
    parser.add_argument('command', choices=['run', 'activate', 'rollback'])
    parser.add_argument('--image', type=pilot.image_id)
    parser.add_argument('--env-file')
    parser.add_argument('--snapshot')
    parser.add_argument('--codex-sandbox', action='store_true', help='Require the explicitly installed, hash-pinned Codex/Sol sandbox policy')
    args = parser.parse_args()
    configure(args.agent, codex_sandbox=args.codex_sandbox)
    if args.command == 'rollback':
        pilot.rollback()
    elif args.command == 'activate':
        activate(args.image, args.env_file, args.snapshot)
    else:
        raise SystemExit(start(args.image))

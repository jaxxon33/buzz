"""Fresh Claude workers: private login, no imported host auth or SSH authority."""
from contextlib import contextmanager, ExitStack
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import socket
import subprocess
import time
import uuid
from urllib.parse import urlsplit

import provision

CLI = '/opt/buzz/native/claude'
HOST_AUTH = frozenset({'ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN', 'ANTHROPIC_BASE_URL',
                       'CLAUDE_CODE_OAUTH_TOKEN', 'CLAUDE_CONFIG_DIR', 'OPENAI_API_KEY',
                       'BUZZ_AGENT_PROVIDER', 'OPENROUTER_API_KEY', 'OPENROUTER_MODEL',
                       'OPENAI_COMPAT_API_KEY', 'OPENAI_COMPAT_BASE_URL', 'OPENAI_COMPAT_MODEL'})


def validate_environment(values):
    if HOST_AUTH.intersection(values):
        raise ValueError('New Claude profiles require their own contained login, without host/API auth')
    if not all(values.get(key) for key in ['BUZZ_PRIVATE_KEY', 'BUZZ_RELAY_URL', 'BUZZ_ACP_MODEL']):
        raise ValueError('Claude profiles require their own Buzz identity, relay and explicit model')
    if len(values['BUZZ_ACP_MODEL']) > 96 or not re.fullmatch(r'claude-[a-z0-9]+(?:-[a-z0-9]+){1,15}', values['BUZZ_ACP_MODEL']):
        raise ValueError('Use an explicit Claude model ID, such as claude-fable-5-2')


def model_policy(model):
    return {'model': model, 'availableModels': [model], 'enforceAvailableModels': True,
            'fallbackModel': [], 'switchModelsOnFlag': False}


def forwarded(pilot):
    return frozenset(key for key in pilot.FORWARDED
                     if key.startswith('BUZZ_ACP_') or key in {
                         'BUZZ_PRIVATE_KEY', 'BUZZ_AUTH_TAG', 'BUZZ_RELAY_URL'}) | {'BUZZ_ACP_MODEL'}


def runtime_args(pilot, image, name=None, entrypoint=None, network='bridge'):
    args = pilot.runtime_args(image, name=name, entrypoint=entrypoint, network=network)
    args[args.index('--memory') + 1] = '4g'
    args[args.index('--pids-limit') + 1] = '512'
    args += ['--mount', f'type=volume,source={pilot.NAME}-home,target=/home/paul',
             '--env', 'CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1',
             '--env', 'DISABLE_AUTOUPDATER=1']
    return args


def public_routes():
    args = []
    for host in ['api.anthropic.com', 'claude.ai', 'platform.claude.com']:
        address = socket.getaddrinfo(host, 443, family=socket.AF_INET,
                                     type=socket.SOCK_STREAM)[0][4][0]
        args += ['--add-host', host + ':' + address]
    return args


def audit(pilot, name, image, network='bridge'):
    """Reject unexpected launch authority without printing secret environment."""
    data = json.loads(pilot.docker(['inspect', name], capture_output=True, text=True).stdout)[0]
    host = data['HostConfig']
    if (data['Image'] != image or data['Config']['User'] != f'{pilot.UID}:{pilot.UID}' or
            host['NetworkMode'] != network or host['PidMode'] or host['IpcMode'] != 'private' or
            host['Privileged'] or not host['ReadonlyRootfs'] or host['CapAdd'] or host['CapDrop'] != ['ALL'] or
            host['SecurityOpt'] != ['no-new-privileges:true'] or data['AppArmorProfile'] != 'docker-default' or
            host['Memory'] != 4 * 1024 ** 3 or host['NanoCpus'] != 2 * 10 ** 9 or host['PidsLimit'] != 512):
        raise ValueError('Claude launch authority differs from its private worker boundary')
    mounts = data['Mounts']
    actual = {(item['Destination'], item.get('Name'), item['RW']) for item in mounts if item['Type'] != 'tmpfs'}
    expected = {('/workspace', pilot.VOLUME, True), ('/home/paul', pilot.NAME + '-home', True)}
    if actual != expected or any(item['Type'] not in {'volume', 'tmpfs'} for item in mounts):
        raise ValueError('Claude must have only its private workspace and private login home')


def start(pilot, image):
    record = provision.load_profile(pilot.AGENT)
    if (record.get('family') != 'claude' or record['image'] != image or
            os.environ.get('BUZZ_ACP_MODEL') != record['model']):
        raise ValueError('Claude runtime image/model differs from its verified profile')
    if not os.environ.get('BUZZ_PRIVATE_KEY') or not os.environ.get('BUZZ_RELAY_URL'):
        raise ValueError('Claude requires its own Buzz environment')
    args = runtime_args(pilot, image)
    args += public_routes()
    relay = urlsplit(os.environ['BUZZ_RELAY_URL']).hostname
    if not relay:
        raise ValueError('Missing relay hostname')
    address = socket.getaddrinfo(relay, 443, family=socket.AF_INET,
                                 type=socket.SOCK_STREAM)[0][4][0]
    bridge = pilot.docker(['network', 'inspect', 'bridge', '--format',
                          '{{(index .IPAM.Config 0).Gateway}}'], capture_output=True, text=True).stdout.strip()
    args += ['--add-host', relay + ':127.0.0.1']
    for key in sorted(key for key in forwarded(pilot) if os.environ.get(key)):
        args += ['--env', key]
    with ExitStack() as cleanup:
        tunnel = cleanup.enter_context(pilot.Tunnel((bridge, 0), (address, 443)))
        port = tunnel.start()
        cleanup.callback(tunnel.shutdown)
        args += ['--env', 'BUZZ_PILOT_RELAY_TARGET=' + json.dumps([bridge, port])]
        return subprocess.call(['docker', *args, image])


@contextmanager
def pending_profile(pilot, agent):
    provision.validate_name(agent)
    with (provision.ROOT / '.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        record = provision.load_profile(agent, states=('awaiting-login',))
        if record.get('family') != 'claude':
            raise ValueError('Only a new pending Claude profile can use contained login')
        pilot.configure_profile(agent, record['uid'], agent)
        for prop, expected in [('MainPID', '0'), ('ActiveState', 'inactive')]:
            if pilot.systemctl('show', pilot.NAME + '.service', '--property=' + prop, '--value').stdout.strip() != expected:
                raise ValueError('Stop the Claude service before completing its login')
        for volume in [pilot.VOLUME, pilot.NAME + '-home']:
            if pilot.docker(['ps', '-q', '--filter', 'volume=' + volume], capture_output=True, text=True).stdout.strip():
                raise ValueError('Private Claude storage is already in use')
        yield record


def remove_helper(pilot, name):
    if pilot.docker(['ps', '-aq', '--filter', 'name=^/' + name + '$'],
                    capture_output=True, text=True, timeout=15).stdout.strip():
        pilot.docker(['rm', '--force', name], capture_output=True, timeout=15)


def helper(pilot, record, name, command, *, network='none', interactive=False, readonly=False, **kwargs):
    if pilot.docker(['ps', '-aq', '--filter', 'name=^/' + name + '$'],
                    capture_output=True, text=True).stdout.strip():
        raise ValueError('Existing Claude helper must be inspected before reuse')
    args = runtime_args(pilot, record['image'], name=name, entrypoint=command[0], network=network)
    if interactive:
        args += ['--interactive', '--tty']
    if readonly:
        for index, value in enumerate(args):
            if value.startswith('type=volume,'):
                args[index] = value + ',readonly,volume-nocopy'
    if network == 'bridge':
        args += public_routes()
    error = None
    try:
        return pilot.docker([*args, record['image'], *command[1:]], **kwargs)
    except BaseException as caught:
        error = caught
        raise
    finally:
        try:
            remove_helper(pilot, name)
        except Exception as cleanup:
            if error:
                error.add_note('Claude helper cleanup needs inspection: ' + type(cleanup).__name__)
            else:
                raise


def login(pilot, agent):
    """Owner-terminal flow; authorization URLs/codes never go into channel logs."""
    if not os.isatty(0) or not os.isatty(1):
        raise ValueError('Run claude-login in your own interactive terminal; do not send login codes in chat')
    with pending_profile(pilot, agent) as record:
        # Docker must allocate the terminal itself, while retaining exactly
        # the same unprivileged boundary and private volumes as the worker.
        helper(pilot, record, pilot.NAME + '-login', [CLI, 'auth', 'login', '--claudeai'],
               interactive=True, network='bridge', timeout=600)


def auth_status(pilot, record):
    try:
        result = helper(pilot, record, pilot.NAME + '-authstatus',
                        [CLI, 'auth', 'status', '--json'], capture_output=True, text=True, timeout=30)
    except subprocess.CalledProcessError:
        raise ValueError('Claude is not signed in; complete its contained login first') from None
    status = json.loads(result.stdout)
    if status.get('loggedIn') is not True or status.get('authMethod') != 'claude.ai':
        raise ValueError('Claude must use its own Claude subscription login')


def model_check(pilot, record):
    label = 'claude-profile-' + uuid.uuid4().hex[:12]
    code = Path(__file__).with_name('acp_probe.py').read_text().split("if __name__ == '__main__':")[0]
    code += '\nprobe(["/opt/buzz/bin/native-adapter"], "/workspace", ' + repr(record['model']) + ', 75, label=' + repr(label) + ', with_mcp=True)\n'
    result = helper(pilot, record, pilot.NAME + '-check', ['python3', '-'], network='bridge',
                    input=code, capture_output=True, text=True, timeout=180)
    records = [json.loads(line) for line in result.stdout.splitlines() if line.startswith('{')]
    if not any(row.get('native_model_tools') == 'passed' and row.get('uid') == record['uid'] for row in records):
        raise ValueError('Claude model/file/Git check did not pass')
    # A separate read-only worker verifies actual bytes/UID and staged blob.
    path = '/workspace/REPOS/' + label
    verify = (f'from pathlib import Path; import subprocess; p=Path({path!r}); '
              f'assert p.joinpath("result.txt").stat().st_uid=={record["uid"]}; '
              'assert p.joinpath("result.txt").read_bytes()==b"370\\n"; '
              'assert subprocess.check_output(["git","-C",str(p),"show",":result.txt"])==b"370\\n"')
    helper(pilot, record, pilot.NAME + '-verify', ['python3', '-c', verify], readonly=True,
           capture_output=True, timeout=30)


def activate(pilot, agent):
    """Only a private login plus exact-model/tool verification unlock startup."""
    with provision.operation_deadline(), pending_profile(pilot, agent) as record:
        source = Path(record['environment_file'])
        if hashlib.sha256(source.read_bytes()).hexdigest() != record['environment_sha256']:
            raise ValueError('Claude settings changed after preparation; inspect before activation')
        auth_status(pilot, record)
        model_check(pilot, record)
        try:
            record['state'] = 'ready'
            provision.write_record(provision.ROOT / (agent + '.json'), record)
            pilot.systemctl('enable', '--now', pilot.NAME + '.service')
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                try:
                    running = pilot.docker(['inspect', '--format', '{{.State.Running}}', pilot.NAME],
                                           capture_output=True, text=True, timeout=5).stdout.strip()
                except subprocess.CalledProcessError:
                    running = ''
                if running == 'true':
                    audit(pilot, pilot.NAME, record['image'])
                    pilot.systemctl('is-active', pilot.NAME + '.service')
                    signal.setitimer(signal.ITIMER_REAL, 0)
                    print(json.dumps({'agent': agent, 'state': 'started', 'family': 'claude',
                                      'uid': record['uid'], 'model': record['model'],
                                      'preflight_verified': True, 'live_relay_verified': False}))
                    return
                time.sleep(0.5)
            raise RuntimeError('Claude container did not become ready')
        except BaseException as error:
            signal.setitimer(signal.ITIMER_REAL, 0)
            record['state'] = 'awaiting-login'
            try:
                provision.write_record(provision.ROOT / (agent + '.json'), record)
            except Exception as cleanup:
                error.add_note('Claude recovery record needs inspection: ' + type(cleanup).__name__)
            try:
                pilot.systemctl('disable', '--now', pilot.NAME + '.service', timeout=30)
            except Exception as cleanup:
                error.add_note('Claude stop needs inspection: ' + type(cleanup).__name__)
            raise

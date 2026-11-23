"""Fresh Antigravity (agy) workers: private Google login, no host auth or keyring.

SSH is off by default. An owner-approved `agy-ssh-grant` gives one ready worker
its own per-agent key for ag only, in a separate volume mounted read-only.

Mirrors claude_profile: provisioning stops at awaiting-login, the owner signs
in from their own terminal inside the worker boundary, and only a verified
login plus a real model/file/Git check enables the isolated unit.
"""
from contextlib import contextmanager, ExitStack
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import subprocess
import tempfile
import time
import uuid
from urllib.parse import urlsplit

import provision
import hive_secrets

CLI = '/opt/buzz/native/agy'
FAMILY = 'agy'
# Gemini API keys, Vertex/ADC and every other provider's auth are refused:
# the only supported path is the worker's own interactive Google login.
HOST_AUTH = frozenset({
    'GEMINI_API_KEY', 'GOOGLE_API_KEY', 'GOOGLE_APPLICATION_CREDENTIALS',
    'GOOGLE_CLOUD_PROJECT', 'GOOGLE_CLOUD_PROJECT_ID', 'GOOGLE_CLOUD_LOCATION',
    'GOOGLE_GENAI_USE_VERTEXAI', 'GOOGLE_GENAI_USE_GCA', 'CLOUDSDK_CONFIG',
    'ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN', 'ANTHROPIC_BASE_URL',
    'CLAUDE_CODE_OAUTH_TOKEN', 'CLAUDE_CONFIG_DIR', 'OPENAI_API_KEY',
    'BUZZ_AGENT_PROVIDER', 'OPENROUTER_API_KEY', 'OPENROUTER_MODEL',
    'OPENAI_COMPAT_API_KEY', 'OPENAI_COMPAT_BASE_URL', 'OPENAI_COMPAT_MODEL',
    'DBUS_SESSION_BUS_ADDRESS', 'XDG_RUNTIME_DIR'})
# The only host an agy worker may be granted, with its pinned Tailscale address.
SSH_HOSTS = {'ag': '100.109.21.86'}
SSH_CONFIG = """Host *
    User paul
    IdentityFile /home/paul/.ssh/id_ed25519
    IdentityAgent none
    IdentitiesOnly yes
    BatchMode yes
    PasswordAuthentication no
    KbdInteractiveAuthentication no
    ForwardAgent no
    ClearAllForwardings yes
    StrictHostKeyChecking yes
    UserKnownHostsFile /home/paul/.ssh/known_hosts
    GlobalKnownHostsFile /dev/null
Host ag
    HostName 100.109.21.86
"""
MODEL = re.compile(r'gemini-[a-z0-9]+(?:[-.][a-z0-9]+){1,15}')


def validate_environment(values):
    if HOST_AUTH.intersection(values):
        raise ValueError('New agy profiles require their own contained Google login, without host/API auth')
    if not all(values.get(key) for key in ['BUZZ_PRIVATE_KEY', 'BUZZ_RELAY_URL', 'BUZZ_ACP_MODEL']):
        raise ValueError('agy profiles require their own Buzz identity, relay and explicit model')
    if len(values['BUZZ_ACP_MODEL']) > 96 or not MODEL.fullmatch(values['BUZZ_ACP_MODEL']):
        raise ValueError('Use an explicit Gemini model ID, such as gemini-4-neon-medium')
    if values.get('BUZZ_ACP_AGENT_ARGS', '') not in {'', 'acp'}:
        raise ValueError('agy profiles run only the pinned image adapter (agy acp)')


def forwarded(pilot):
    return frozenset(key for key in pilot.FORWARDED
                     if key.startswith('BUZZ_ACP_') or key in {
                         'BUZZ_PRIVATE_KEY', 'BUZZ_AUTH_TAG', 'BUZZ_RELAY_URL'}) | {'BUZZ_ACP_MODEL'}


def ssh_granted(record):
    hosts = record.get('ssh')
    if hosts is None:
        return False
    if hosts != sorted(SSH_HOSTS):
        raise ValueError('agy profile names an unapproved SSH grant')
    return True


def runtime_args(pilot, image, name=None, entrypoint=None, network='bridge', ssh=False):
    args = pilot.runtime_args(image, name=name, entrypoint=entrypoint, network=network)
    args[args.index('--memory') + 1] = '4g'
    args[args.index('--pids-limit') + 1] = '512'
    # No D-Bus/keyring is reachable, so agy keeps its login in the private home.
    args += ['--mount', f'type=volume,source={pilot.NAME}-home,target=/home/paul']
    if ssh:
        # Only the running worker gets it; login/check helpers never do.
        args += ['--mount', f'type=volume,source={pilot.NAME}-ssh,target=/home/paul/.ssh,readonly,volume-nocopy']
    return args


def audit(pilot, name, image, network='bridge', ssh=False):
    """Reject unexpected launch authority without printing secret environment."""
    data = json.loads(pilot.docker(['inspect', name], capture_output=True, text=True).stdout)[0]
    host = data['HostConfig']
    if (data['Image'] != image or data['Config']['User'] != f'{pilot.UID}:{pilot.UID}' or
            host['NetworkMode'] != network or host['PidMode'] or host['IpcMode'] != 'private' or
            host['Privileged'] or not host['ReadonlyRootfs'] or host['CapAdd'] or host['CapDrop'] != ['ALL'] or
            host['SecurityOpt'] != ['no-new-privileges:true'] or data['AppArmorProfile'] != 'docker-default' or
            host['Memory'] != 4 * 1024 ** 3 or host['NanoCpus'] != 2 * 10 ** 9 or host['PidsLimit'] != 512):
        raise ValueError('agy launch authority differs from its private worker boundary')
    mounts = hive_secrets.audit_mounts(data['Mounts'], pilot.AGENT)
    actual = {(item['Destination'], item.get('Name'), item['RW']) for item in mounts if item['Type'] != 'tmpfs'}
    expected = {('/workspace', pilot.VOLUME, True), ('/home/paul', pilot.NAME + '-home', True)}
    if ssh:
        expected.add(('/home/paul/.ssh', pilot.NAME + '-ssh', False))
    if actual != expected or any(item['Type'] not in {'volume', 'tmpfs'} for item in mounts):
        raise ValueError('agy must have only its private workspace, login home and granted SSH')
    environment = {item.split('=', 1)[0] for item in data['Config'].get('Env') or []}
    if environment.intersection(HOST_AUTH):
        raise ValueError('agy launch carries host or API authentication')


def start(pilot, image):
    record = provision.load_profile(pilot.AGENT)
    if (record.get('family') != FAMILY or record['image'] != image or
            os.environ.get('BUZZ_ACP_MODEL') != record['model']):
        raise ValueError('agy runtime image/model differs from its verified profile')
    if not os.environ.get('BUZZ_PRIVATE_KEY') or not os.environ.get('BUZZ_RELAY_URL'):
        raise ValueError('agy requires its own Buzz environment')
    args = runtime_args(pilot, image, ssh=ssh_granted(record))
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
        args += cleanup.enter_context(hive_secrets.delivery(pilot.AGENT, pilot.NAME))
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
        if record.get('family') != FAMILY:
            raise ValueError('Only a new pending agy profile can use contained agy login')
        pilot.configure_profile(agent, record['uid'], agent)
        for prop, expected in [('MainPID', '0'), ('ActiveState', 'inactive')]:
            if pilot.systemctl('show', pilot.NAME + '.service', '--property=' + prop, '--value').stdout.strip() != expected:
                raise ValueError('Stop the agy service before completing its login')
        for volume in [pilot.VOLUME, pilot.NAME + '-home']:
            if pilot.docker(['ps', '-q', '--filter', 'volume=' + volume], capture_output=True, text=True).stdout.strip():
                raise ValueError('Private agy storage is already in use')
        yield record


def remove_helper(pilot, name):
    if pilot.docker(['ps', '-aq', '--filter', 'name=^/' + name + '$'],
                    capture_output=True, text=True, timeout=15).stdout.strip():
        pilot.docker(['rm', '--force', name], capture_output=True, timeout=15)


def helper(pilot, record, name, command, *, network='none', interactive=False, readonly=False, **kwargs):
    if pilot.docker(['ps', '-aq', '--filter', 'name=^/' + name + '$'],
                    capture_output=True, text=True).stdout.strip():
        raise ValueError('Existing agy helper must be inspected before reuse')
    args = runtime_args(pilot, record['image'], name=name, entrypoint=command[0], network=network)
    if interactive or kwargs.get('input') is not None:
        args += ['--interactive']
    if interactive:
        args += ['--tty']
    if readonly:
        for index, value in enumerate(args):
            if value.startswith('type=volume,'):
                args[index] = value + ',readonly,volume-nocopy'
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
                error.add_note('agy helper cleanup needs inspection: ' + type(cleanup).__name__)
            else:
                raise


def login(pilot, agent):
    """Owner-terminal flow; authorization URLs/codes never go into channel logs."""
    if not os.isatty(0) or not os.isatty(1):
        raise ValueError('Run agy-login in your own interactive terminal; do not send login codes in chat')
    with pending_profile(pilot, agent) as record:
        # Official ACP terminal login: prints a Google URL and reads the
        # pasted code in this terminal, then stores it in the private home.
        helper(pilot, record, pilot.NAME + '-login', [CLI, 'acp', '--login', '--disable-telemetry'],
               interactive=True, network='bridge', timeout=600)


# Runs inside the worker: an authenticated session/new is the login check.
STATUS = r'''
import json, os, selectors, subprocess, sys, time
child = subprocess.Popen(["/opt/buzz/bin/native-adapter"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.DEVNULL, cwd="/workspace")
selector = selectors.DefaultSelector(); selector.register(child.stdout, selectors.EVENT_READ)
pending = b""
def request(number, method, params):
    global pending
    child.stdin.write((json.dumps({"jsonrpc": "2.0", "id": number, "method": method, "params": params}) + "\n").encode())
    child.stdin.flush()
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        if b"\n" not in pending:
            if not selector.select(max(0, deadline - time.monotonic())):
                break
            chunk = os.read(child.stdout.fileno(), 65536)
            if not chunk:
                raise SystemExit("adapter exited")
            pending += chunk
            continue
        line, pending = pending.split(b"\n", 1)
        message = json.loads(line)
        if message.get("id") == number and "method" not in message:
            return message
    raise SystemExit("deadline")
try:
    init = request(1, "initialize", {"protocolVersion": 1, "clientInfo": {"name": "buzz-isolation-status", "version": "1"},
                                     "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False}, "terminal": False}})
    session = request(2, "session/new", {"cwd": "/workspace", "mcpServers": []})
    result = session.get("result") or {}
    models = sorted({m.get("modelId") for m in (result.get("models") or {}).get("availableModels", []) if m.get("modelId")} |
                    {v.get("value") for o in result.get("configOptions", []) if o.get("category") == "model"
                     for v in o.get("options", []) if v.get("value")})
    print(json.dumps({"agent": (init.get("result") or {}).get("agentInfo", {}).get("name"),
                      "logged_in": bool(result.get("sessionId")),
                      "error_code": (session.get("error") or {}).get("code"), "models": models}))
finally:
    child.terminate()
'''


def auth_status(pilot, record):
    result = helper(pilot, record, pilot.NAME + '-authstatus', ['python3', '-'], network='bridge',
                    input=STATUS, capture_output=True, text=True, timeout=90)
    rows = [json.loads(line) for line in result.stdout.splitlines() if line.startswith('{')]
    if not rows or rows[-1].get('logged_in') is not True:
        raise ValueError('agy is not signed in; complete its contained login first')
    if record['model'] not in rows[-1].get('models', []):
        raise ValueError('agy login does not offer the requested model ' + record['model'])
    return rows[-1]


def model_check(pilot, record):
    label = 'agy-profile-' + uuid.uuid4().hex[:12]
    code = Path(__file__).with_name('acp_probe.py').read_text().split("if __name__ == '__main__':")[0]
    code += '\nprobe(["/opt/buzz/bin/native-adapter"], "/workspace", ' + repr(record['model']) + ', 120, label=' + repr(label) + ', with_mcp=True)\n'
    result = helper(pilot, record, pilot.NAME + '-check', ['python3', '-'], network='bridge',
                    input=code, capture_output=True, text=True, timeout=240)
    records = [json.loads(line) for line in result.stdout.splitlines() if line.startswith('{')]
    if not any(row.get('native_model_tools') == 'passed' and row.get('uid') == record['uid'] for row in records):
        raise ValueError('agy model/file/Git check did not pass')
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
            raise ValueError('agy settings changed after preparation; inspect before activation')
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
                    audit(pilot, pilot.NAME, record['image'], ssh=ssh_granted(record))
                    pilot.systemctl('is-active', pilot.NAME + '.service')
                    signal.setitimer(signal.ITIMER_REAL, 0)
                    print(json.dumps({'agent': agent, 'state': 'started', 'family': FAMILY,
                                      'uid': record['uid'], 'model': record['model'],
                                      'preflight_verified': True, 'live_relay_verified': False}))
                    return
                time.sleep(0.5)
            raise RuntimeError('agy container did not become ready')
        except BaseException as error:
            signal.setitimer(signal.ITIMER_REAL, 0)
            record['state'] = 'awaiting-login'
            try:
                provision.write_record(provision.ROOT / (agent + '.json'), record)
            except Exception as cleanup:
                error.add_note('agy recovery record needs inspection: ' + type(cleanup).__name__)
            try:
                pilot.systemctl('disable', '--now', pilot.NAME + '.service', timeout=30)
            except Exception as cleanup:
                error.add_note('agy stop needs inspection: ' + type(cleanup).__name__)
            raise


def ssh_grant(pilot, agent, home=Path('/home/paul')):
    """Owner-approved: copy this agent's own key and the ag pin into its SSH volume."""
    provision.validate_name(agent)
    with (provision.ROOT / '.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        path = provision.ROOT / (agent + '.json')
        record = provision.load_profile(agent)
        if record.get('family') != FAMILY:
            raise ValueError('agy-ssh-grant is only for a ready agy profile')
        # Hive edits a ready agent's settings (e.g. messaging) in place, so
        # only the current display name matters here; it selects the key.
        display = provision.read_environment(Path(record['environment_file'])).get('BUZZ_ACP_DISPLAY_NAME', '')
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}', display):
            raise ValueError('agy profile has no usable display name for its key')
        pilot.configure_profile(agent, record['uid'], display)
        key = home / '.ssh/agents' / display
        info = key.lstat() if key.exists() or key.is_symlink() else None
        if info is None or not key.is_file() or key.is_symlink() or info.st_mode & 0o077:
            raise ValueError('Per-agent key is missing or not a private regular file')
        if not (home / '.ssh/agents' / (display + '.pub')).is_file():
            raise ValueError('Per-agent public key is missing')
        pins = subprocess.run(['ssh-keygen', '-F', SSH_HOSTS['ag'], '-f', str(home / '.ssh/known_hosts')],
                              capture_output=True, text=True, check=True).stdout
        pins = [line for line in pins.splitlines() if line and not line.startswith('#')]
        if len(pins) != 1 or pins[0].split()[1] != 'ssh-ed25519':
            raise ValueError('Expected exactly one ed25519 host pin for ag')
        with tempfile.TemporaryDirectory(prefix='buzz-agy-ssh-') as directory:
            staged = Path(directory) / 'ssh'
            staged.mkdir(mode=0o700)
            shutil.copyfile(key, staged / 'id_ed25519')
            (staged / 'id_ed25519').chmod(0o400)
            (staged / 'known_hosts').write_text(pins[0] + '\n')
            (staged / 'config').write_text(SSH_CONFIG)
            for name in ['known_hosts', 'config']:
                (staged / name).chmod(0o444)
            volume = pilot.NAME + '-ssh'
            pilot.docker(['volume', 'create', '--label', pilot.LABEL, volume], capture_output=True)
            previous = pilot.VOLUME
            try:
                pilot.VOLUME = volume
                pilot.migrate_workspace(record['image'], staged)
            finally:
                pilot.VOLUME = previous
        record['ssh'] = sorted(SSH_HOSTS)
        provision.write_record(path, record)
    pilot.systemctl('restart', pilot.NAME + '.service')
    print(json.dumps({'agent': agent, 'ssh': record['ssh'], 'restarted': True}))

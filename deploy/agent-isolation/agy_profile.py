"""Fresh Antigravity (agy) workers: private Google login, no host auth, keyring or SSH.

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
import signal
import socket
import subprocess
import time
import uuid
from urllib.parse import urlsplit

import provision
import hive_secrets
import shared_prompt

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


def runtime_args(pilot, image, name=None, entrypoint=None, network='bridge'):
    args = pilot.runtime_args(image, name=name, entrypoint=entrypoint, network=network)
    args[args.index('--memory') + 1] = '4g'
    args[args.index('--pids-limit') + 1] = '512'
    # No D-Bus/keyring is reachable, so agy keeps its login in the private home.
    args += ['--mount', f'type=volume,source={pilot.NAME}-home,target=/home/paul']
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
        raise ValueError('agy launch authority differs from its private worker boundary')
    mounts = shared_prompt.audit_mounts(hive_secrets.audit_mounts(data['Mounts'], pilot.AGENT))
    actual = {(item['Destination'], item.get('Name'), item['RW']) for item in mounts if item['Type'] != 'tmpfs'}
    expected = {('/workspace', pilot.VOLUME, True), ('/home/paul', pilot.NAME + '-home', True)}
    if actual != expected or any(item['Type'] not in {'volume', 'tmpfs'} for item in mounts):
        raise ValueError('agy must have only its private workspace and private login home')
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
    args = runtime_args(pilot, image)
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
        args += shared_prompt.launch_args()
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
                    audit(pilot, pilot.NAME, record['image'])
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

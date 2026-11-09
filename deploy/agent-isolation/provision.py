"""Trusted host provisioning for new buzz-agent workers; never runs in a worker.

Reservations survive failures. No error path starts an uncontained host agent.
The invoking operator/service owns the profile registry and Docker access.
"""
import fcntl
from contextlib import contextmanager
import grp
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import shlex
import signal
import stat
import subprocess
import tempfile
import time

ROOT = Path.home() / '.config/buzz-agent-isolation/profiles'
UID_START, UID_END = 62000, 64999


def validate_name(agent):
    if not re.fullmatch(r'[a-z][a-z0-9-]{0,47}', agent):
        raise ValueError('Agent name must be 1-48 lowercase letters, digits or hyphens')


def load_profile(agent, states=('ready',)):
    validate_name(agent)
    record = json.loads((ROOT / (agent + '.json')).read_text())
    if (record.get('agent') != agent or record.get('state') not in states or
            record.get('family', 'buzz-agent') not in {'buzz-agent', 'claude'} or
            type(record.get('uid')) is not int or
            not UID_START <= record['uid'] <= UID_END or
            not re.fullmatch(r'sha256:[a-f0-9]{64}', record.get('image', ''))):
        raise ValueError('Profile is not a successfully provisioned agent')
    return record


def write_record(path, record):
    descriptor, filename = tempfile.mkstemp(prefix=path.stem + '.', suffix='.tmp', dir=path.parent)
    temporary = Path(filename)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            json.dump(record, stream, sort_keys=True, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def operation_deadline(seconds=12 * 60):
    """Leave time for failure recording/stop before the caller's 15min timeout."""
    def expired(*_):
        raise TimeoutError('Isolated provisioning exceeded its operation deadline')

    previous_handler = signal.signal(signal.SIGALRM, expired)
    previous_timer = signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, *previous_timer)
        signal.signal(signal.SIGALRM, previous_handler)


def read_environment(path):
    """Accept Hive's simple systemd assignment format without shell evaluation."""
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or
            info.st_mode & 0o077):
        raise ValueError('Environment must be an owned private regular file')
    values = {}
    for line in path.read_text().splitlines():
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        key, separator, raw = line.partition('=')
        if not separator or not re.fullmatch('[A-Z][A-Z0-9_]*', key) or key in values:
            raise ValueError('Unsupported environment assignment')
        # An unquoted value extends to end-of-line in systemd EnvironmentFile.
        if raw.startswith(('"', "'")):
            parsed = shlex.split(raw, comments=False)
            if len(parsed) != 1:
                raise ValueError('Unsupported quoted environment value')
            raw = parsed[0]
        if any(c in raw for c in '\x00\r\n'):
            raise ValueError('Unsupported environment value')
        values[key] = raw
    command = Path(values.get('BUZZ_ACP_AGENT_COMMAND', '')).name
    if command == 'claude-agent-acp':
        import claude_profile
        claude_profile.validate_environment(values)
        return values
    provider = values.get('BUZZ_AGENT_PROVIDER')
    if provider not in {'openai', 'openrouter'}:
        raise ValueError('Only buzz-agent openai/openrouter provisioning is supported')
    if Path(values.get('BUZZ_ACP_AGENT_COMMAND', '')).name != 'buzz-agent':
        raise ValueError('Native adapters require a separately verified isolation profile')
    required = {'BUZZ_PRIVATE_KEY', 'BUZZ_RELAY_URL'}
    required |= ({'OPENAI_COMPAT_API_KEY', 'OPENAI_COMPAT_BASE_URL', 'OPENAI_COMPAT_MODEL'}
                 if provider == 'openai' else {'OPENROUTER_API_KEY', 'OPENROUTER_MODEL'})
    if not all(values.get(key) for key in required):
        raise ValueError('Incomplete agent/provider environment')
    return values


def inspect_base(pilot, image, family='buzz-agent'):
    pilot.image_id(image)
    config = json.loads(pilot.docker(['image', 'inspect', image, '--format', '{{json .Config}}'],
                                    capture_output=True, text=True).stdout)
    environment = dict(item.split('=', 1) for item in config.get('Env', []))
    expected = {
        'HOME': '/workspace', 'BUZZ_NEST': '/workspace',
        'BUZZ_ACP_AGENT_COMMAND': '/opt/buzz/bin/buzz-agent', 'BUZZ_ACP_AGENT_ARGS': '',
        'BUZZ_ACP_MCP_COMMAND': '/opt/buzz/bin/buzz-dev-mcp',
        'BUZZ_ACP_BASE_PROMPT_FILE': '/opt/buzz/base-prompt.md',
    }
    forbidden = set(pilot.FORWARDED)
    if family == 'claude':
        import claude_profile
        expected.update(HOME='/home/paul', BUZZ_ACP_AGENT_COMMAND='/opt/buzz/bin/native-adapter')
        forbidden.update(claude_profile.HOST_AUTH)
        if (config.get('Labels') or {}).get('buzz.isolation.native') != 'claude':
            raise ValueError('Claude provisioning requires the pinned credential-free Claude image')
    if (config.get('Entrypoint') != ['python3', '/opt/buzz/contained.py'] or
            config.get('WorkingDir') != '/workspace' or config.get('Volumes') or
            config.get('Cmd') or
            any(environment.get(key) != value for key, value in expected.items()) or
            set(environment).intersection(forbidden) or
            (config.get('Labels') or {}).get('buzz.isolation.desktop')):
        raise ValueError('Base must be the pinned common credential-free buzz-agent image')
    user = config.get('User', '')
    if not re.fullmatch(r'6[0-9]{4}:6[0-9]{4}', user) or len(set(user.split(':'))) != 1:
        raise ValueError('Base image worker identity is unsupported')


def service_text(pilot, source, image, declared, family='buzz-agent'):
    script = Path(pilot.__file__).resolve()
    if any(c in str(p) for p in (script, source) for c in ' \t\n\r%"\\'):
        raise ValueError('Unsupported character in unit path')
    forwarded = pilot.FORWARDED
    if family == 'claude':
        import claude_profile
        forwarded = claude_profile.forwarded(pilot)
    unset = ' '.join(sorted(forwarded - declared))
    return f'''[Unit]
Description=Isolated provisioned agent {pilot.AGENT}
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
Environment=PATH=/usr/local/bin:/usr/bin:/bin
EnvironmentFile={source}
UnsetEnvironment={unset}
ExecStart=/usr/bin/python3 -E -s "{script}" --agent {pilot.AGENT} run --image {image}
ExecStop=/usr/bin/docker stop --time 25 {pilot.NAME}
Restart=on-failure
RestartSec=15
TimeoutStopSec=45
UMask=0077
StandardOutput=null
StandardError=journal

[Install]
WantedBy=default.target
'''


def provision(pilot, agent, env_file, base):
    """Reserve one identity, create empty private storage and start it contained."""
    with operation_deadline():
        return _provision(pilot, agent, env_file, base)


def _provision(pilot, agent, env_file, base):
    validate_name(agent)
    # Native profiles are reserved too, without importing their credential code.
    if agent in pilot.PROFILES or agent in {
            'sol', 'codex', 'cursor', 'claude-haiku', 'claude-sonnet', 'claude-opus',
            'claude', 'omp', 'opus', 'luna', 'fable'}:
        raise ValueError('Existing agent names cannot be provisioned')
    source = Path(env_file)
    if (not source.is_absolute() or source.is_symlink() or
            source.name != 'buzz-acp-' + agent + '.env'):
        raise ValueError('Use an absolute non-symlink environment file')
    source = source.resolve(strict=True)
    values = read_environment(source)
    family = 'claude' if Path(values['BUZZ_ACP_AGENT_COMMAND']).name == 'claude-agent-acp' else 'buzz-agent'
    inspect_base(pilot, base, family)
    unit_dir = Path.home() / '.config/systemd/user'
    unit = unit_dir / ('buzz-isolated-' + agent + '.service')
    # All allocators use this lock, held through activation/failure recording.
    ROOT.mkdir(parents=True, mode=0o700, exist_ok=True)
    with (ROOT / '.lock').open('a') as lock:
        os.chmod(ROOT / '.lock', 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        record_path = ROOT / (agent + '.json')
        if record_path.exists() or record_path.is_symlink() or unit.exists() or unit.is_symlink():
            raise ValueError('Agent profile or replacement unit already exists')
        nest = pilot.WORKSPACE_ROOT / agent
        if nest.is_symlink() or (nest.exists() and (not nest.is_dir() or any(nest.iterdir()))):
            raise ValueError('Existing agent workspace must not be reused')
        original = 'buzz-acp@' + agent + '.service'
        for prop, expected in [('MainPID', '0'), ('ActiveState', 'inactive')]:
            if pilot.systemctl('show', original, '--property=' + prop, '--value').stdout.strip() != expected:
                raise ValueError('Existing host service is not stopped')
        state = pilot.systemctl('show', original, '--property=UnitFileState', '--value').stdout.strip()
        if state not in {'disabled', 'masked', ''}:
            raise ValueError('Existing host service could start on boot')
        volume = 'buzz-isolated-' + agent + '-workspace'
        volumes = [volume] + (['buzz-isolated-' + agent + '-home'] if family == 'claude' else [])
        queries = [['volume', 'ls', '--filter', 'name=^' + name + '$', '--format', '{{.Name}}'] for name in volumes]
        for query in queries + [
                      ['ps', '-aq', '--filter', 'name=^/buzz-isolated-' + agent + '$'],
                      ['ps', '-aq', '--filter', 'name=^/buzz-isolated-' + agent + '-init$']]:
            if pilot.docker(query, capture_output=True, text=True).stdout.strip():
                raise ValueError('Existing container or workspace volume must not be reused')
        used = {uid for uid, _ in pilot.PROFILES.values()}
        used.update(row.pw_uid for row in pwd.getpwall())
        used.update(row.gr_gid for row in grp.getgrall())
        for path in ROOT.glob('*.json'):
            used.add(json.loads(path.read_text())['uid'])
        uid = next((number for number in range(UID_START, UID_END + 1) if number not in used), None)
        if uid is None:
            raise ValueError('No unreserved worker UID remains')
        record = {'agent': agent, 'uid': uid, 'base': base, 'state': 'preparing', 'family': family}
        write_record(record_path, record)
        pilot.configure_profile(agent, uid, agent)
        wrote_unit = False
        try:
            # BuildKit interprets bare sha256 IDs in FROM as registry names.
            # Use a local alias derived from the digest, and verify the pin.
            local_base = 'buzz-provision-base:' + base.split(':')[1]
            pilot.docker(['tag', base, local_base], capture_output=True)
            with tempfile.TemporaryDirectory(prefix='buzz-provision-') as context:
                account = 'native-worker' if family == 'claude' else 'buzz-worker'
                roots = '/workspace /home/paul' if family == 'claude' else '/workspace'
                policy = ''
                if family == 'claude':
                    import claude_profile
                    Path(context, 'managed-settings.json').write_text(json.dumps(claude_profile.model_policy(values['BUZZ_ACP_MODEL'])))
                    policy = ('COPY --chmod=0444 managed-settings.json /etc/claude-code/managed-settings.json\n'
                              'RUN chmod 0755 /etc/claude-code && chown 0:0 /etc/claude-code /etc/claude-code/managed-settings.json\n')
                Path(context, 'Dockerfile').write_text(f'''FROM {local_base}
USER 0:0
RUN /usr/sbin/groupmod --gid {uid} {account} && /usr/sbin/usermod --uid {uid} --gid {uid} {account} && chown {uid}:{uid} {roots}
{policy}USER {uid}:{uid}
''')
                iid = Path(context) / 'image-id'
                pilot.docker(['build', '--network', 'none', '--iidfile', str(iid), context],
                             capture_output=True, text=True, timeout=600)
                image = pilot.image_id(iid.read_text().strip())
            actual_base = pilot.docker(['image', 'inspect', local_base, '--format', '{{.Id}}'],
                                      capture_output=True, text=True).stdout.strip()
            if actual_base != base:
                raise ValueError('Pinned base reference changed while building')
            actual_user = pilot.docker(['image', 'inspect', image, '--format', '{{.Config.User}}'],
                                      capture_output=True, text=True).stdout.strip()
            if actual_user != f'{uid}:{uid}':
                raise ValueError('Derived image has the wrong worker identity')
            for name in volumes:
                pilot.docker(['volume', 'create', '--label', pilot.LABEL, name], capture_output=True)
            # Docker copies /workspace metadata from this image into a NEW volume.
            # Check actual ownership and emptiness without granting root or a host bind.
            code = ('import os; from pathlib import Path; p=Path("/workspace"); '
                    f'assert os.getuid()=={uid} and p.stat().st_uid=={uid}; '
                    'assert not list(p.iterdir()); '
                    'p.joinpath(".write-probe").write_bytes(b"ok"); p.joinpath(".write-probe").unlink()')
            args = pilot.runtime_args(image, name=pilot.NAME + '-init', entrypoint='python3', network='none')
            if family == 'claude':
                args = claude_profile.runtime_args(pilot, image, name=pilot.NAME + '-init', entrypoint='python3', network='none')
                code += ('; h=Path("/home/paul"); '
                         f'assert h.stat().st_uid=={uid} and not list(h.iterdir()); '
                         'h.joinpath(".write-probe").write_bytes(b"ok"); h.joinpath(".write-probe").unlink(); '
                         'import json; policy=Path("/etc/claude-code/managed-settings.json"); '
                         f'assert json.loads(policy.read_text())=={claude_profile.model_policy(values["BUZZ_ACP_MODEL"])!r}; '
                         'assert policy.stat().st_uid==0 and not (policy.stat().st_mode & 0o222)')
            pilot.docker([*args, image, '-c', code], capture_output=True, text=True)
            unit_dir.mkdir(parents=True, exist_ok=True)
            with unit.open('x') as stream:
                wrote_unit = True
                os.chmod(unit, 0o600)
                stream.write(service_text(pilot, source, image, set(values), family))
            if family == 'claude':
                record.update(image=image, state='awaiting-login', model=values['BUZZ_ACP_MODEL'],
                              environment_file=str(source),
                              environment_sha256=hashlib.sha256(source.read_bytes()).hexdigest())
                write_record(record_path, record)
                pilot.systemctl('daemon-reload')
                signal.setitimer(signal.ITIMER_REAL, 0)
                print(json.dumps({'agent': agent, 'uid': uid, 'image': image, 'family': family,
                                  'state': 'awaiting-login', 'service': pilot.NAME + '.service',
                                  'workflow_verified': False}, sort_keys=True))
                return
            record.update(image=image, state='ready')
            write_record(record_path, record)
            pilot.systemctl('daemon-reload')
            pilot.systemctl('enable', '--now', pilot.NAME + '.service')
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                try:
                    check = pilot.docker(['inspect', '--format', '{{.State.Running}}', pilot.NAME],
                                         capture_output=True, text=True, timeout=10)
                except subprocess.CalledProcessError:
                    check = None
                if check and check.stdout.strip() == 'true':
                    break
                time.sleep(0.5)
            else:
                raise RuntimeError('Provisioned container did not start')
            pilot.systemctl('is-active', pilot.NAME + '.service')
            # Startup is committed. Do not report an operation timeout during
            # the success message after the verified service has started.
            signal.setitimer(signal.ITIMER_REAL, 0)
        except BaseException as error:
            # Recovery has its own short command bounds. An operation alarm
            # firing between these steps must not skip the remaining cleanup.
            signal.setitimer(signal.ITIMER_REAL, 0)
            # Keep reservations/storage for deliberate operator recovery. Never
            # enable buzz-acp@, erase a possibly live volume or retry under host UID.
            record['state'] = 'failed'
            try:
                write_record(record_path, record)
            except Exception as cleanup_error:
                error.add_note('Recovery record update failed: ' + type(cleanup_error).__name__)
            # Killing the Docker client does not stop its daemon-side container.
            # This exact helper name was absent before this reservation.
            try:
                helper = pilot.NAME + '-init'
                pending = pilot.docker(['ps', '-aq', '--filter', 'name=^/' + helper + '$'],
                                       capture_output=True, text=True, timeout=15)
                if pending.stdout.strip():
                    pilot.docker(['rm', '--force', helper], capture_output=True, timeout=15)
            except Exception as cleanup_error:
                error.add_note('Init helper cleanup needs operator inspection: ' + type(cleanup_error).__name__)
            if wrote_unit:
                try:
                    pilot.systemctl('disable', '--now', pilot.NAME + '.service', timeout=30)
                except Exception as cleanup_error:
                    error.add_note('Replacement stop needs operator inspection: ' + type(cleanup_error).__name__)
            raise
        print(json.dumps({'agent': agent, 'uid': uid, 'image': image, 'volume': volume,
                          'service': pilot.NAME + '.service', 'state': 'started',
                          'workflow_verified': False}, sort_keys=True))

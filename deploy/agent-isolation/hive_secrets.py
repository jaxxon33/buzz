"""Host-only encrypted credentials and narrowly scoped tmpfs delivery.

Never import this module into a worker image. Exceptions deliberately exclude
crypto output and plaintext. The host user/root and a granted agent are trusted.
"""
from contextlib import contextmanager, ExitStack, nullcontext
from datetime import datetime, timezone
import fcntl
import getpass
import json
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import subprocess
import tempfile
import time
import warnings

LIMIT = 64 * 1024


class SecretError(ValueError):
    """A redacted operator error; safe to print without a traceback."""


def validate_name(name):
    if not isinstance(name, str) or not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,62}', name):
        raise SecretError('Secret names must be 1-63 lowercase letters, digits or hyphens')
    return name


def validate_agent(agent):
    import pilot
    import native
    import provision
    provision.validate_name(agent)
    if agent not in pilot.PROFILES and agent not in native.PROFILES:
        provision.load_profile(agent, states=('ready', 'awaiting-login', 'archived'))
    return agent


def private_dir(path, create=False):
    """Require owned directories and reject symlinks in every path component."""
    path = Path(path)
    if not path.is_absolute():
        raise SecretError('Secret paths must be absolute')
    for part in [*reversed(path.parents), path]:
        if not part.exists() and not part.is_symlink() and create:
            part.mkdir(mode=0o700)
        info = part.lstat()
        if not stat.S_ISDIR(info.st_mode):
            raise SecretError('Secret paths must not contain symlinks')
    info = path.lstat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise SecretError('Secret directory must be owned by the operator with mode 0700')
    return path


def read_private(path):
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), 'rb') as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or
                stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > LIMIT):
            raise SecretError('Secret files must be owned regular files with mode 0600 and bounded size')
        raw = stream.read(LIMIT + 1)
        if len(raw) > LIMIT:
            raise SecretError('Secret file is too large')
        return raw


def atomic_write(path, raw):
    """Persist a private file and directory entry without a plaintext temp file."""
    fd, name = tempfile.mkstemp(prefix='.' + path.name + '-', dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        sync_dir(path.parent)
    finally:
        Path(name).unlink(missing_ok=True)


def sync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


@contextmanager
def file_lock(path, nonblocking=False, timeout=5):
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise SecretError('Unsafe secrets lock')
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if nonblocking or time.monotonic() >= deadline:
                    raise SecretError('Secrets lock is busy; retry the operation') from None
                time.sleep(min(0.05, max(0, deadline - time.monotonic())))
        yield
    finally:
        os.close(fd)


def crypto(tool, args=(), data=None):
    binary = shutil.which(tool)
    if not binary:
        raise SecretError('Install age and age-keygen on the host before using secrets')
    try:
        result = subprocess.run([binary, *args], input=data, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, timeout=15,
                                env={'PATH': os.defpath, 'HOME': '/nonexistent'})
    except (OSError, subprocess.SubprocessError):
        raise SecretError('Age operation failed; inspect the host installation') from None
    if result.returncode or len(result.stdout) > LIMIT:
        raise SecretError('Age operation failed; check the identity or encrypted secret')
    return result.stdout


def fields_document(raw):
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError):
        raise SecretError('Invalid encrypted secret document') from None
    if not isinstance(value, dict) or set(value) != {'fields'} or not isinstance(value['fields'], dict):
        raise SecretError('Invalid secret fields')
    fields = value['fields']
    if not fields or len(fields) > 32:
        raise SecretError('A secret needs 1-32 fields')
    for key, item in fields.items():
        validate_field(key)
        if not isinstance(item, str):
            raise SecretError('Secret field values must be strings')
    return value


def validate_field(field):
    if not isinstance(field, str) or not 1 <= len(field) <= 64 or not field.isprintable():
        raise SecretError('Field names must be 1-64 printable characters')
    return field


class Store:
    """Operator-owned age store; no API prints or returns plaintext to the CLI."""

    def __init__(self, home=None):
        home = Path.home() if home is None else Path(home)
        self.root = home / '.config/hive-secrets'
        self.encrypted = self.root / 'secrets'
        self.state = home / '.local/state/hive-secrets'
        self.identity = self.state / 'identity.txt'
        self.grants_path = self.root / 'grants.json'

    def check_dirs(self):
        for path in [self.root, self.encrypted]:
            private_dir(path)

    def init(self):
        # Generate first: a missing/broken age installation leaves no store.
        if self.identity.exists() or self.identity.is_symlink():
            raise SecretError('Identity already exists; refusing to overwrite it')
        raw = crypto('age-keygen')
        for path in [self.root, self.encrypted, self.state]:
            private_dir(path, create=True)
        with file_lock(self.root / '.lock'):
            if self.identity.exists() or self.identity.is_symlink():
                raise SecretError('Identity already exists; refusing to overwrite it')
            if any(self.encrypted.iterdir()) or self.grants():
                raise SecretError('Store already contains data; restore its matching identity')
            # Empty grants are harmless without an identity. A crash here is
            # resumable by init and must never poison unrelated agent starts.
            self.save_grants({})
            atomic_write(self.identity, raw)

    @contextmanager
    def locked(self):
        self.check_dirs()
        with file_lock(self.root / '.lock'):
            yield self

    def identity_path(self):
        private_dir(self.state)
        read_private(self.identity)
        return str(self.identity)

    def grants(self):
        if not self.grants_path.exists() and not self.grants_path.is_symlink():
            return {}
        private_dir(self.root)
        try:
            value = json.loads(read_private(self.grants_path))
        except (ValueError, UnicodeError):
            raise SecretError('Invalid secrets grant registry') from None
        if not isinstance(value, dict) or len(value) > 256:
            raise SecretError('Invalid secrets grant registry')
        for name, row in value.items():
            validate_name(name)
            if (not isinstance(row, dict) or set(row) != {'agents', 'updated'} or
                    not isinstance(row['agents'], list) or len(row['agents']) > 256 or
                    not isinstance(row['updated'], str) or len(row['updated']) > 40):
                raise SecretError('Invalid secrets grant record')
            # Do not resolve historical grants against a mutable live registry:
            # retiring another agent must not break this agent's startup.
            for agent in row['agents']:
                if not isinstance(agent, str) or not re.fullmatch(r'[a-z][a-z0-9-]{0,47}', agent):
                    raise SecretError('Invalid agent name in secrets grant')
        return value

    def save_grants(self, value):
        raw = (json.dumps(value, sort_keys=True) + '\n').encode()
        if len(raw) > LIMIT:
            raise SecretError('Too many secret grants')
        atomic_write(self.grants_path, raw)

    def decrypt(self, name):
        raw = read_private(self.encrypted / (validate_name(name) + '.age'))
        value = crypto('age', ['--decrypt', '--identity', self.identity_path()], raw)
        fields_document(value)
        return value

    def set(self, name, fields, agents=None):
        validate_name(name)
        value = json.dumps({'fields': fields}).encode()
        if len(value) > LIMIT - 4096:
            raise SecretError('Secret is too large')
        fields_document(value)
        if agents is not None:
            agents = sorted(set(validate_agent(a) for a in agents))
        grants = self.grants()
        wanted = agents if agents is not None else grants.get(name, {}).get('agents', [])
        # Encrypt before any mutation. Revoking old grants first makes every
        # crash prefix fail closed even though ciphertext and grants are files.
        encrypted = crypto('age', ['--encrypt', '--identity', self.identity_path()], value)
        grants.pop(name, None)
        self.save_grants(grants)
        atomic_write(self.encrypted / (name + '.age'), encrypted)
        grants[name] = {'agents': wanted, 'updated': datetime.now(timezone.utc).isoformat()}
        self.save_grants(grants)

    def grant(self, name, agent, revoke=False):
        validate_name(name)
        if not revoke:
            validate_agent(agent)
        elif not re.fullmatch(r'[a-z][a-z0-9-]{0,47}', agent):
            raise SecretError('Invalid agent name')
        grants = self.grants()
        if name not in grants:
            raise SecretError('Secret does not have a grant record')
        # Refuse to grant unreadable/corrupt ciphertext; revoke remains possible.
        if not revoke:
            self.decrypt(name)
        agents = set(grants[name]['agents'])
        agents.discard(agent) if revoke else agents.add(agent)
        grants[name] = {'agents': sorted(agents), 'updated': datetime.now(timezone.utc).isoformat()}
        self.save_grants(grants)

    def remove(self, name):
        validate_name(name)
        grants = self.grants()
        grants.pop(name, None)
        self.save_grants(grants)
        (self.encrypted / (name + '.age')).unlink(missing_ok=True)
        sync_dir(self.encrypted)

    def metadata(self):
        grants = self.grants()
        names = set(grants)
        for path in self.encrypted.iterdir():
            if path.name.endswith('.age'):
                names.add(validate_name(path.stem))
        rows = []
        for name in sorted(names):
            row = {'name': name, 'agents': grants.get(name, {}).get('agents', []),
                   'updated': grants.get(name, {}).get('updated')}
            try:
                row['fields'] = sorted(fields_document(self.decrypt(name))['fields'])
            except (OSError, SecretError):
                row.update(fields=[], error='unreadable')
            rows.append(row)
        return rows

    def revision(self, name):
        path = self.encrypted / (validate_name(name) + '.age')
        raw = read_private(path) if path.exists() or path.is_symlink() else None
        return raw, self.grants().get(name)


def runtime_root():
    raw = os.environ.get('XDG_RUNTIME_DIR', '/run/user/' + str(os.getuid()))
    path = private_dir(Path(raw))
    # Check the actual backing filesystem, not whether the pathname says /run.
    info = subprocess.run(['findmnt', '--noheadings', '--output', 'FSTYPE', '--target', str(path)],
                          check=True, capture_output=True, text=True, timeout=5)
    if info.stdout.strip() != 'tmpfs':
        raise SecretError('Secret staging requires an owned mode-0700 tmpfs runtime directory')
    return path / 'hive-secrets'


def container_exists(name):
    result = subprocess.run(['docker', 'ps', '--all', '--quiet', '--filter', 'name=^/' + name + '$'],
                            check=True, capture_output=True, text=True, timeout=10)
    return bool(result.stdout.strip())


@contextmanager
def termination_cleanup():
    """Make catchable service termination unwind the secrets lease/leaf."""
    def terminate(signum, frame):
        raise SystemExit(128 + signum)
    previous = signal.signal(signal.SIGTERM, terminate)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)


@contextmanager
def delivery(agent, container_name):
    """Yield only a read-only mount; plaintext is never part of Docker args."""
    import provision
    provision.validate_name(agent)
    store = Store()
    candidate = (Path(os.environ.get('XDG_RUNTIME_DIR', '/run/user/' + str(os.getuid()))) /
                 'hive-secrets' / agent)
    stale = candidate.exists() or candidate.is_symlink()
    names = sorted(name for name, row in store.grants().items() if agent in row['agents'])
    if not names and not stale:
        yield []
        return
    mount = []
    with ExitStack() as cleanup:
        # A short global lock gives a coherent snapshot. The per-agent lease
        # stays held through launch/exit, while CLI updates remain available.
        cleanup.enter_context(termination_cleanup())
        with store.locked() if names else nullcontext():
            names = sorted(name for name, row in store.grants().items() if agent in row['agents'])
            if names or stale:
                root = private_dir(runtime_root(), create=True)
                cleanup.enter_context(file_lock(root / (agent + '.lock'), nonblocking=True))
                if container_exists(container_name):
                    raise SecretError('Existing container must be stopped before preparing secrets')
                leaf = root / agent
                if leaf.is_symlink():
                    raise SecretError('Unsafe secrets staging directory')
                if leaf.exists():
                    shutil.rmtree(leaf)
                if names:
                    leaf.mkdir(mode=0o755)
                    leaf.chmod(0o755)  # do not inherit a restrictive service umask
                    cleanup.callback(shutil.rmtree, leaf)
                    for name in names:
                        try:
                            raw = store.decrypt(name)
                        except (OSError, ValueError, subprocess.SubprocessError):
                            raise SecretError('Cannot prepare granted secret: ' + name) from None
                        path = leaf / (name + '.json')
                        with path.open('xb') as stream:
                            stream.write(raw)
                        path.chmod(0o444)
                    mount = ['--mount', f'type=bind,source={leaf},target=/run/secrets,readonly']
        yield mount


def audit_mounts(mounts, agent):
    """Validate the one permitted bind mount and return all other mounts."""
    remaining = []
    found = False
    for mount in mounts:
        if mount['Destination'] != '/run/secrets':
            remaining.append(mount)
            continue
        if found or (mount['Type'], mount['RW'], mount.get('Source')) != (
                'bind', False, str(runtime_root() / agent)):
            raise SecretError('Unexpected secrets mount authority')
        found = True
    return remaining


def require_terminal():
    if not os.isatty(0) or not os.isatty(1):
        raise SecretError('Use an interactive host terminal; never send credentials in chat, argv or environment')


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description='Host-only encrypted secrets; values are never printed')
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('init')
    commands.add_parser('list')
    p = commands.add_parser('set')
    p.add_argument('name', type=validate_name)
    p.add_argument('--field', action='append', type=validate_field)
    p.add_argument('--agent', action='append', type=validate_agent)
    for command in ['grant', 'revoke']:
        p = commands.add_parser(command)
        p.add_argument('name', type=validate_name)
        p.add_argument('agent')
    p = commands.add_parser('rm')
    p.add_argument('name', type=validate_name)
    args = parser.parse_args(argv)
    os.umask(0o077)
    store = Store()
    if args.command == 'init':
        store.init()
        print('Store initialized. Keep the identity separate from encrypted-store backups.')
        return
    if args.command in {'set', 'rm'}:
        require_terminal()
        with store.locked():
            revision = store.revision(args.name)
        if args.command == 'rm' or revision[0] is not None:
            verb = 'Remove' if args.command == 'rm' else 'Overwrite'
            if input(verb + ' ' + args.name + '? [y/N] ').lower() != 'y':
                return
        if args.command == 'set':
            fields = args.field or ['username', 'password']
            if len(set(fields)) != len(fields) or not 1 <= len(fields) <= 32:
                raise SecretError('Choose 1-32 distinct fields')
            with warnings.catch_warnings():
                warnings.simplefilter('error', getpass.GetPassWarning)
                try:
                    values = {field: getpass.getpass(field + ': ') for field in fields}
                except getpass.GetPassWarning:
                    raise SecretError('Cannot disable terminal echo; no secret was accepted') from None
        with store.locked():
            if store.revision(args.name) != revision:
                raise SecretError('Secret changed during entry; retry and confirm the new state')
            if args.command == 'set':
                store.set(args.name, values, args.agent)
                print('Secret saved. Grants and updated values take effect at the next agent restart.')
            else:
                store.remove(args.name)
                print('Secret removed. Restart affected agents and rotate the password at the provider.')
        return
    with store.locked():
        if args.command == 'list':
            print(json.dumps(store.metadata(), indent=2))
        else:
            store.grant(args.name, args.agent, revoke=args.command == 'revoke')
            print('Grant change takes effect at the next agent restart.')
            if args.command == 'revoke':
                print('Rotate the password at the provider; a running or previously granted agent may retain it.')

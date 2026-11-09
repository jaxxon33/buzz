"""Explicit, hash-pinned host policy for the selected Codex/Sol containers.

Policy installation/loading is an administrator action. This module only reads
the two fixed root-owned files and supplies Docker's per-container options.
"""
import hashlib
import json
import os
from pathlib import Path
import stat

ROOT = Path('/etc/buzz-agent-isolation')
APPARMOR = 'buzz-native-codex-v1'
PROFILE = Path('/etc/apparmor.d') / APPARMOR
FILES = {
    'codex-apparmor-v1.profile': '7e1a7e8fbfa2789d26959fad50b4a6164e76017c8013f6925a6d155b82128cc6',
    'codex-seccomp-v1.json': '70070ddc9bd28a3133aafd370397ca57db870b4b3113877bae40e5e69955a8e1',
}


def _read_verified():
    """Fail closed on missing, writable, substituted or symlinked policy."""
    for directory in [ROOT.parent, ROOT, PROFILE.parent]:
        metadata = directory.lstat()
        if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != 0 or metadata.st_mode & 0o022:
            raise ValueError('Policy directory must be root-owned and not writable by workers')
    contents = {}
    paths = {name: ROOT / name for name in FILES}
    paths['autoload-profile'] = PROFILE
    for name, path in paths.items():
        expected = FILES['codex-apparmor-v1.profile'] if name == 'autoload-profile' else FILES[name]
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, 'rb') as stream:
            metadata = os.fstat(stream.fileno())
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != 0 or metadata.st_mode & 0o022:
                raise ValueError('Policy must be a root-owned regular file, not writable by workers')
            contents[name] = stream.read()
            if hashlib.sha256(contents[name]).hexdigest() != expected:
                raise ValueError('Installed sandbox policy differs from reviewed bytes')
    return contents


def verify():
    _read_verified()
    return dict(FILES)


def security_options():
    verify()
    return ['apparmor=' + APPARMOR, 'seccomp=' + str(ROOT / 'codex-seccomp-v1.json')]


def audit_options(options, profile):
    """Docker records seccomp JSON, after its CLI expands the filename."""
    contents = _read_verified()
    fixed = {'no-new-privileges:true', 'apparmor=' + APPARMOR}
    if len(options) != 3 or not fixed.issubset(options) or profile != APPARMOR:
        raise ValueError('Container security policy does not match the selected native boundary')
    dynamic = [option for option in options if option not in fixed]
    if len(dynamic) != 1 or not dynamic[0].startswith('seccomp='):
        raise ValueError('Missing exact native seccomp policy')
    try:
        actual = json.loads(dynamic[0].removeprefix('seccomp='))
    except ValueError as error:
        raise ValueError('Docker did not record the native seccomp policy') from error
    if actual != json.loads(contents['codex-seccomp-v1.json']):
        raise ValueError('Container seccomp policy differs from reviewed policy')

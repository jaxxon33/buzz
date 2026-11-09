#!/usr/bin/env python3
"""Copy selected agent state into private volumes; never print its contents."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import tempfile

import native
import pilot
import workspace
import native_sandbox


def copy(source, target):
    if not source.exists():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    if source.is_dir():
        shutil.copytree(source, target, symlinks=True)
    else:
        shutil.copy2(source, target)


def transfer(image, source, volume):
    pilot.docker(['volume', 'create', '--label', pilot.LABEL, volume], capture_output=True)
    previous = pilot.VOLUME
    try:
        pilot.VOLUME = volume
        pilot.migrate_workspace(image, source)
    finally:
        pilot.VOLUME = previous


def prepare(agent, image, snapshot, codex_sandbox=False):
    family = native.configure(agent, codex_sandbox=codex_sandbox)
    policy = native_sandbox.verify() if codex_sandbox else None
    source_home = Path('/home/paul')
    source = workspace.validate(pilot.WORKSPACE_ROOT / agent)
    before = workspace.digest(source)
    snapshot = Path(snapshot)
    if snapshot.exists():
        raise ValueError('Refusing to replace an existing migration record')
    pid = int(pilot.systemctl('show', pilot.ORIGINAL_SERVICE, '-p', 'MainPID', '--value').stdout)
    if not pid:
        raise ValueError('Original agent is not running')
    env = dict(x.split('=', 1) for x in Path(f'/proc/{pid}/environ').read_text().split('\0') if '=' in x)
    with tempfile.TemporaryDirectory(prefix='buzz-native-state-') as directory:
        home = Path(directory) / 'home'
        home.mkdir(mode=0o700)
        if family == 'claude':
            copy(source_home / '.claude/.credentials.json', home / '.claude/.credentials.json')
            copy(source_home / '.claude/settings.json', home / '.claude/settings.json')
            project = '-home-paul-buzz-nests-' + agent
            copy(source_home / '.claude/projects' / project, home / '.claude/projects' / project)
            config = json.loads((source_home / '.claude.json').read_text())
            config['projects'] = {k: v for k, v in config.get('projects', {}).items()
                                  if k == str(source) or k.startswith(str(source) + '/')}
            (home / '.claude.json').write_text(json.dumps(config) + '\n')
        elif family == 'codex':
            codex_home = Path(env.get('CODEX_HOME', str(source_home / '.codex')))
            if not codex_home.is_relative_to(source_home):
                raise ValueError('Codex state must be under the original home')
            target = home / codex_home.relative_to(source_home)
            # The managed per-agent homes are private. The default home is
            # shared with interactive use: import auth/config, not others' chats.
            if codex_home == source_home / '.codex':
                for name in ['auth.json', 'config.toml', 'skills', 'plugins']:
                    copy(codex_home / name, target / name)
            else:
                copy(codex_home, target)
            # The host setup shares only these cache files by symlink. Copy
            # their bytes into the private home; never retain a host-home link.
            for name in ['auth.json', 'models_cache.json', 'installation_id']:
                destination = target / name
                if destination.is_symlink():
                    if destination.resolve().parent != source_home / '.codex':
                        raise ValueError('Unexpected shared Codex state target')
                    destination.unlink()
                    shutil.copy2(codex_home / name, destination)
            # Runtime-generated wrappers are recreated by Codex itself and
            # point at the old executable. They are not durable user state.
            if (target / 'tmp').exists():
                shutil.rmtree(target / 'tmp')
        elif family == 'cursor':
            for name in ['.config/cursor/auth.json', '.cursor/cli-config.json', '.cursor/acp-config.json']:
                copy(source_home / name, home / name)
        else:
            copy(source_home / '.omp/agent', home / '.omp/agent')
            copy(source_home / '.omp/natives', home / '.omp/natives')
        workspace.validate(home)
        transfer(image, home, pilot.NAME + '-home')
        # Provision only this agent's phase-1 key and public host pins. The
        # running worker receives this separate volume read-only.
        ssh = Path(directory) / 'ssh'
        ssh.mkdir(mode=0o700)
        copy(source_home / '.ssh/agents' / pilot.DISPLAY_NAME, ssh / 'id_ed25519')
        copy(source_home / '.ssh/known_hosts', ssh / 'known_hosts')
        if not (ssh / 'id_ed25519').is_file():
            raise ValueError('Selected agent key is missing')
        (ssh / 'id_ed25519').chmod(0o400)
        (ssh / 'config').write_text('''Host *
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
Host m2
    HostName 100.66.252.42
Host m4
    HostName 100.72.87.107
''')
        transfer(image, ssh, pilot.NAME + '-ssh')
        if agent == 'claude-opus':
            desktop = Path(directory) / 'desktop'
            desktop.mkdir(mode=0o700)
            # This is the exact key path in this agent's saved desktop MCP
            # configuration, copied privately and mounted read-only.
            copy(source_home / '.config/desktop-mcp/opus', desktop / 'opus')
            if not (desktop / 'opus').is_file():
                raise ValueError('Claude-Opus desktop key is missing')
            (desktop / 'opus').chmod(0o400)
            transfer(image, desktop, pilot.NAME + '-desktop')
    transfer(image, source, pilot.VOLUME)
    if workspace.digest(source) != before:
        raise ValueError('Workspace changed while preparing; keep original running')
    env_file = source_home / '.config/buzz-agents' / f'buzz-acp-{agent}.env'
    snapshot.write_text(json.dumps({'agent': agent, 'image': image, 'workspace': before,
                                   'original_pid': pid,
                                   'environment_file_sha256': hashlib.sha256(env_file.read_bytes()).hexdigest(),
                                   'sandbox_policy': policy}, indent=2) + '\n')
    print('Private native state and workspace copies verified')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--agent', required=True, choices=native.PROFILES)
    parser.add_argument('--image', required=True, type=pilot.image_id)
    parser.add_argument('--snapshot', required=True)
    parser.add_argument('--codex-sandbox', action='store_true')
    args = parser.parse_args()
    prepare(args.agent, args.image, args.snapshot, args.codex_sandbox)

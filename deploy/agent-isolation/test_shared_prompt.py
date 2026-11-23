"""The shared base prompt reaches every live launch read-only, and audits accept nothing else."""
from contextlib import nullcontext
import copy
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

import agy_profile
import claude_profile
import hive_secrets
import native
import pilot
import provision
import shared_prompt

IMAGE = 'sha256:' + 'a' * 64
SOURCE = str(shared_prompt.SOURCE)
PROMPT = {'Type': 'bind', 'Source': SOURCE, 'Destination': '/opt/buzz/shared', 'RW': False}


def volume(destination, name, writable):
    return {'Type': 'volume', 'Destination': destination, 'Name': name, 'RW': writable}


# Each mount mutation must be rejected by every production audit.
REJECTED = {
    'writable': [{**PROMPT, 'RW': True}],
    'other-source': [{**PROMPT, 'Source': '/home/paul/.config/buzz-agents'}],
    'other-target': [{**PROMPT, 'Destination': '/opt/buzz/other'}],
    'volume-at-target': [{**PROMPT, 'Type': 'volume', 'Name': 'x'}],
    'duplicate': [PROMPT, PROMPT],
    'other-bind': [PROMPT, {'Type': 'bind', 'Source': '/etc', 'Destination': '/mnt/etc', 'RW': False}],
}


class LaunchArgs(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(subprocess.run, ['rm', '-rf', str(self.root)])
        self.source = self.root / 'buzz-shared-prompt'
        patcher = mock.patch.object(shared_prompt, 'SOURCE', self.source)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_real_file_is_mounted_readonly_and_selected(self):
        self.source.mkdir()
        self.source.chmod(0o755)
        (self.source / 'base-prompt.md').write_text('rules\n')
        (self.source / 'base-prompt.md').chmod(0o644)
        self.assertEqual(shared_prompt.launch_args(), [
            '--mount', f'type=bind,source={self.source},target=/opt/buzz/shared,readonly',
            '--env', 'BUZZ_ACP_BASE_PROMPT_FILE=/opt/buzz/shared/base-prompt.md'])

    def test_unusable_source_falls_back_to_the_baked_prompt_with_a_warning(self):
        real = self.root / 'real'
        real.mkdir()
        (real / 'base-prompt.md').write_text('rules\n')

        def missing_dir():
            pass

        def empty_file():
            self.source.mkdir()
            (self.source / 'base-prompt.md').write_text('')

        def missing_file():
            self.source.mkdir()

        def symlinked_dir():
            self.source.symlink_to(real)

        def symlinked_file():
            self.source.mkdir()
            (self.source / 'base-prompt.md').symlink_to(real / 'base-prompt.md')

        def directory_as_file():
            (self.source / 'base-prompt.md').mkdir(parents=True)

        def unreadable_file():
            # An atomic save (temp file + rename) commonly leaves mode 0600.
            self.source.mkdir()
            (self.source / 'base-prompt.md').write_text('rules\n')
            (self.source / 'base-prompt.md').chmod(0o600)

        def untraversable_dir():
            self.source.mkdir()
            (self.source / 'base-prompt.md').write_text('rules\n')
            self.source.chmod(0o750)

        for case in [missing_dir, empty_file, missing_file, symlinked_dir, symlinked_file, directory_as_file,
                     unreadable_file, untraversable_dir]:
            with self.subTest(case=case.__name__):
                if self.source.is_dir() and not self.source.is_symlink():
                    self.source.chmod(0o755)
                subprocess.run(['rm', '-rf', str(self.source)], check=True)
                case()
                with mock.patch('sys.stderr') as stderr:
                    self.assertEqual(shared_prompt.launch_args(), [])
                self.assertIn('using the baked prompt', ''.join(c.args[0] for c in stderr.write.call_args_list))


class MountAudits(unittest.TestCase):
    def setUp(self):
        self.addCleanup(pilot.configure, pilot.AGENT)
        patcher = mock.patch.object(hive_secrets, 'runtime_root', return_value=Path('/run/user/1000/hive-secrets'))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_helper_passes_only_the_exact_mount_through(self):
        other = volume('/workspace', 'w', True)
        self.assertEqual(shared_prompt.audit_mounts([other, PROMPT]), [other])
        self.assertEqual(shared_prompt.audit_mounts([other]), [other])
        for name, mounts in REJECTED.items():
            if name in {'other-target', 'other-bind'}:
                continue  # Not the prompt target: passed through for the caller's own check.
            with self.subTest(name=name), self.assertRaises(ValueError):
                shared_prompt.audit_mounts(mounts)

    def check(self, audit, data):
        """Exact mount alone passes, absent passes, and every mutation fails."""
        base = copy.deepcopy(data['Mounts'])

        def run(mounts):
            data['Mounts'] = base + mounts
            with mock.patch.object(pilot, 'docker', return_value=subprocess.CompletedProcess(
                    [], 0, stdout=json.dumps([data]))), mock.patch('builtins.print'):
                audit()

        run([])
        run([PROMPT])
        for name, mounts in REJECTED.items():
            with self.subTest(name=name), self.assertRaises(ValueError):
                run(mounts)

    def boundary(self, uid, mounts, **host):
        return {'Image': IMAGE, 'Config': {'User': f'{uid}:{uid}', 'Env': []},
                'AppArmorProfile': 'docker-default', 'Mounts': mounts,
                'HostConfig': {'SecurityOpt': ['no-new-privileges:true'], 'NetworkMode': 'bridge',
                               'PidMode': '', 'IpcMode': 'private', 'Privileged': False,
                               'ReadonlyRootfs': True, 'CapAdd': None, 'CapDrop': ['ALL'], **host}}

    def test_native_audit(self):
        native.configure('claude-haiku')
        prefix = pilot.NAME
        data = self.boundary(pilot.UID, [
            volume('/workspace', prefix + '-workspace', True),
            volume('/home/paul/buzz/nests/claude-haiku', prefix + '-workspace', True),
            volume('/home/paul', prefix + '-home', True),
            volume('/home/paul/.ssh', prefix + '-ssh', False)])
        self.check(lambda: native.audit(pilot.NAME), data)

    def test_claude_and_agy_audits(self):
        for module in [claude_profile, agy_profile]:
            with self.subTest(module=module.__name__):
                pilot.configure_profile('synthetic', 62009, 'synthetic')
                data = self.boundary(62009, [
                    volume('/workspace', pilot.VOLUME, True),
                    volume('/home/paul', pilot.NAME + '-home', True)],
                    Memory=4 * 1024 ** 3, NanoCpus=2 * 10 ** 9, PidsLimit=512)
                self.check(lambda: module.audit(pilot, pilot.NAME, IMAGE), data)

    def test_agy_audit_with_and_without_the_ssh_grant(self):
        pilot.configure_profile('synthetic', 62009, 'synthetic')
        ssh = volume('/home/paul/.ssh', pilot.NAME + '-ssh', False)
        base = [volume('/workspace', pilot.VOLUME, True), volume('/home/paul', pilot.NAME + '-home', True)]
        cases = [
            ('prompt-alone-no-grant', [PROMPT], False, True),
            ('prompt-and-ssh-with-grant', [PROMPT, ssh], True, True),
            ('ssh-without-grant', [PROMPT, ssh], False, False),
            ('grant-without-ssh', [PROMPT], True, False),
            ('other-bind-with-grant', [PROMPT, ssh, REJECTED['other-bind'][1]], True, False),
        ]
        for name, mounts, granted, passes in cases:
            with self.subTest(name=name):
                data = self.boundary(62009, base + mounts, Memory=4 * 1024 ** 3,
                                     NanoCpus=2 * 10 ** 9, PidsLimit=512)
                with mock.patch.object(pilot, 'docker', return_value=subprocess.CompletedProcess(
                        [], 0, stdout=json.dumps([data]))):
                    if passes:
                        agy_profile.audit(pilot, pilot.NAME, IMAGE, ssh=granted)
                    else:
                        with self.assertRaises(ValueError):
                            agy_profile.audit(pilot, pilot.NAME, IMAGE, ssh=granted)


class LaunchPaths(unittest.TestCase):
    def test_every_production_launch_path_adds_the_prompt_mount(self):
        self.addCleanup(pilot.configure, pilot.AGENT)
        sentinel = ['--mount', 'synthetic-shared-prompt']
        env = {'BUZZ_PRIVATE_KEY': 'synthetic', 'BUZZ_RELAY_URL': 'wss://relay.invalid',
               'BUZZ_AGENT_PROVIDER': 'openrouter', 'OPENROUTER_API_KEY': 'synthetic',
               'OPENROUTER_MODEL': 'synthetic', 'BUZZ_ACP_MODEL': 'synthetic-model'}
        for family in ['pilot', 'native', 'claude', 'agy']:
            with self.subTest(family=family):
                if family == 'pilot':
                    pilot.configure('north')
                elif family == 'native':
                    native.configure('codex')
                else:
                    pilot.configure_profile('synthetic', 62009, 'synthetic')

                def docker(args, **kwargs):
                    value = 'codex' if family == 'native' and args[0] == 'image' else '127.0.0.1'
                    return subprocess.CompletedProcess(args, 0, stdout=value)

                launched = []
                with mock.patch.dict(os.environ, env), \
                        mock.patch.object(shared_prompt, 'launch_args', return_value=sentinel), \
                        mock.patch.object(hive_secrets, 'delivery', return_value=nullcontext([])), \
                        mock.patch.object(pilot, 'docker', side_effect=docker), \
                        mock.patch.object(pilot.socket, 'getaddrinfo', return_value=[(2, 1, 6, '', ('127.0.0.1', 443))]), \
                        mock.patch.object(provision, 'load_profile', return_value={
                            'family': family, 'image': IMAGE, 'model': env['BUZZ_ACP_MODEL']}), \
                        mock.patch.object(claude_profile, 'public_routes', return_value=[]), \
                        mock.patch.object(pilot.subprocess, 'call', side_effect=lambda a: launched.append(a) or 0):
                    if family == 'pilot':
                        pilot.start(IMAGE)
                    elif family == 'native':
                        native.start(IMAGE)
                    elif family == 'claude':
                        claude_profile.start(pilot, IMAGE)
                    else:
                        agy_profile.start(pilot, IMAGE)
                args = launched[0]
                self.assertEqual(args[-1], IMAGE)
                index = args.index('synthetic-shared-prompt')
                self.assertEqual(args[index - 1], '--mount')


if __name__ == '__main__':
    unittest.main()

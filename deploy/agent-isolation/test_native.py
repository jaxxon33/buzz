"""Whole native-runtime launch contract and real negative controls."""
import os
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock
import uuid

import native
import pilot


class NativeConfiguration(unittest.TestCase):
    def test_profiles_keep_workspace_paths_and_separate_home_and_readonly_key(self):
        original = pilot.AGENT
        self.addCleanup(pilot.configure, original)
        seen = []
        for agent, (uid, display, _) in native.PROFILES.items():
            native.configure(agent)
            args = native.runtime_args('sha256:' + 'a' * 64)
            self.assertEqual(args[args.index('--user') + 1], f'{uid}:{uid}')
            self.assertIn(f'type=volume,source=buzz-isolated-{agent}-home,target=/home/paul,volume-nocopy', args)
            self.assertIn(f'type=volume,source=buzz-isolated-{agent}-ssh,target=/home/paul/.ssh,readonly,volume-nocopy', args)
            self.assertIn(f'type=volume,source=buzz-isolated-{agent}-workspace,target=/home/paul/buzz/nests/{agent},volume-nocopy', args)
            self.assertFalse(any('type=bind' in arg for arg in args))
            seen.append((pilot.NAME, uid))
        self.assertEqual(len(set(seen)), len(native.PROFILES))
        self.assertEqual(len({x[1] for x in seen}), len(native.PROFILES))


class NativeMigrationJourney(unittest.TestCase):
    def test_settings_snapshot_and_failed_replacement_never_lose_work_or_duplicate_identity(self):
        original = pilot.AGENT
        self.addCleanup(pilot.configure, original)
        native.configure('claude-haiku')
        image = 'sha256:' + 'a' * 64
        for scenario in ['changed-settings', 'failed-start', 'replacement-still-running']:
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as directory:
                home = Path(directory)
                (home / '.config/systemd/user').mkdir(parents=True)
                root = home / 'nests'
                source = root / pilot.AGENT
                source.mkdir(parents=True)
                (source / 'work.txt').write_text('original work\n')
                env_file = home / 'agent.env'
                env_file.write_text('BUZZ_PRIVATE_KEY=synthetic-key\n')
                record = {'agent': pilot.AGENT, 'image': image, 'original_pid': 123,
                          'workspace': native.workspace.digest(source),
                          'environment_file_sha256': hashlib.sha256(env_file.read_bytes()).hexdigest()}
                snapshot = home / 'snapshot.json'
                snapshot.write_text(json.dumps(record))
                if scenario == 'changed-settings':
                    env_file.write_text('BUZZ_PRIVATE_KEY=changed-synthetic-key\n')
                calls = []

                def service(*args):
                    calls.append(args)
                    if args[:2] == ('show', pilot.ORIGINAL_SERVICE):
                        value = '123' if 'MainPID' in args else 'active'
                        return subprocess.CompletedProcess(args, 0, stdout=value)
                    if args == ('enable', '--now', pilot.NAME + '.service'):
                        raise RuntimeError('synthetic failed replacement')
                    return subprocess.CompletedProcess(args, 0, stdout='')

                def docker(args, **kwargs):
                    value = ''
                    if '{{.Config.User}}' in args:
                        value = f'{pilot.UID}:{pilot.UID}'
                    if args[0] == 'ps' and scenario == 'replacement-still-running':
                        value = 'synthetic-live-container'
                    return subprocess.CompletedProcess(args, 0, stdout=value)

                # Once stopped, the original PID is zero. Keep exact ordering
                # observable so restoring the old identity cannot mask a leak.
                original_service = service
                def service_with_stop(*args):
                    if args == ('show', pilot.ORIGINAL_SERVICE, '-p', 'MainPID', '--value') and \
                            ('disable', '--now', pilot.ORIGINAL_SERVICE) in calls:
                        calls.append(args)
                        return subprocess.CompletedProcess(args, 0, stdout='0')
                    return original_service(*args)

                with mock.patch.object(pilot.Path, 'home', return_value=home), \
                        mock.patch.object(pilot, 'WORKSPACE_ROOT', root), \
                        mock.patch.object(pilot, 'systemctl', side_effect=service_with_stop), \
                        mock.patch.object(pilot, 'docker', side_effect=docker):
                    with self.assertRaises((ValueError, RuntimeError)):
                        native.activate(image, env_file, snapshot)
                self.assertEqual((source / 'work.txt').read_text(), 'original work\n')
                restore = ('enable', '--now', pilot.ORIGINAL_SERVICE)
                if scenario == 'changed-settings':
                    self.assertNotIn(('disable', '--now', pilot.ORIGINAL_SERVICE), calls)
                elif scenario == 'failed-start':
                    self.assertLess(calls.index(('disable', '--now', pilot.NAME + '.service')), calls.index(restore))
                else:
                    self.assertNotIn(restore, calls)


@unittest.skipUnless(os.environ.get('NATIVE_IMAGE'), 'Set NATIVE_IMAGE for real native boundary negative controls')
class NativeBoundaryJourney(unittest.TestCase):
    def test_real_launch_and_mutated_authority(self):
        original = pilot.AGENT
        self.addCleanup(pilot.configure, original)
        native.configure(os.environ.get('NATIVE_AGENT', 'claude-haiku'))
        image = pilot.image_id(os.environ['NATIVE_IMAGE'])
        prefix = 'buzz-native-test-' + uuid.uuid4().hex[:12]
        volumes = [prefix + suffix for suffix in ['-workspace', '-home', '-ssh']]
        for volume in volumes:
            pilot.docker(['volume', 'create', volume], capture_output=True)
            self.addCleanup(subprocess.run, ['docker', 'volume', 'rm', volume], capture_output=True, timeout=20)
        # Production provisioning chowns even an empty workspace. Its ownership
        # must survive mounting it at both the legacy path and /workspace.
        pilot.docker(['run', '--rm', '--network', 'none', '--user', '0', '--read-only',
                      '--cap-drop', 'ALL', '--cap-add', 'CHOWN',
                      '--mount', f'type=volume,source={prefix}-workspace,target=/workspace,volume-nocopy',
                      '--mount', f'type=volume,source={prefix}-home,target=/home/paul,volume-nocopy',
                      '--entrypoint', 'python3', image, '-c',
                      f'import os; os.chown("/workspace",{pilot.UID},{pilot.UID}); os.chown("/home/paul",{pilot.UID},{pilot.UID})'],
                     capture_output=True)
        mutations = [None, '--network', '--pid', '--read-only', '--cap-add']
        for index, mutation in enumerate(mutations):
            name = prefix + '-' + str(index)
            with self.subTest(mutation=mutation):
                args = native.runtime_args(image, name=name, entrypoint='sleep', volume_prefix=prefix)
                if mutation == '--network':
                    args[args.index('--network') + 1] = 'host'
                    # net.* sysctls cannot be used with host networking.
                    at = args.index('--sysctl')
                    del args[at:at + 2]
                elif mutation == '--pid':
                    args += ['--pid', 'host']
                elif mutation == '--read-only':
                    args.remove('--read-only')
                elif mutation == '--cap-add':
                    args += ['--cap-add', 'CHOWN']
                pilot.docker([*args, '--detach', image, '60'], capture_output=True)
                try:
                    if mutation:
                        with self.assertRaises(ValueError):
                            native.audit(name, prefix=prefix)
                    else:
                        native.audit(name, prefix=prefix)
                        result = pilot.docker(['exec', name, 'python3', '-c', '''
import os,pathlib
assert os.getuid() != 0
s=pathlib.Path('/proc/self/status').read_text()
assert 'CapEff:\\t0000000000000000' in s and 'NoNewPrivs:\\t1' in s
assert not pathlib.Path('/var/run/docker.sock').exists()
pathlib.Path('worker-write-canary').write_text('prepared volume is writable')
pathlib.Path('/home/paul/home-write-canary').write_text('private home is writable')
try:
 pathlib.Path('/opt/buzz/native-write-canary').touch()
 raise AssertionError('image writable')
except OSError: pass
print('native_shell_boundary=passed')
'''], capture_output=True, text=True)
                        self.assertIn('native_shell_boundary=passed', result.stdout)
                finally:
                    pilot.docker(['rm', '--force', name], capture_output=True)

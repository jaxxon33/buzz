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
import native_sandbox
import pilot


class NativeConfiguration(unittest.TestCase):
    def test_explicit_policy_is_pinned_to_selected_agents_and_snapshot(self):
        self.addCleanup(native.configure, 'claude-haiku')
        image = 'sha256:' + 'a' * 64
        for agent in ['codex', 'sol']:
            native.configure(agent, codex_sandbox=True)
            with mock.patch.object(native_sandbox, 'verify', return_value=dict(native_sandbox.FILES)) as verified:
                args = native.runtime_args(image)
                self.assertIn('apparmor=' + native_sandbox.APPARMOR, args)
                self.assertIn('seccomp=' + str(native_sandbox.ROOT / 'codex-seccomp-v1.json'), args)
                verified.assert_called_once()
            with mock.patch.object(native_sandbox, 'verify', side_effect=ValueError('changed installed policy')):
                with self.assertRaises(ValueError):
                    native.runtime_args(image)
        for agent in set(native.PROFILES) - {'codex', 'sol'}:
            with self.assertRaises(ValueError):
                native.configure(agent, codex_sandbox=True)
        native.configure('codex')
        self.assertFalse(any(arg.startswith(('apparmor=', 'seccomp=')) for arg in native.runtime_args(image)))

    def test_installed_policy_integrity_and_daemon_inspection(self):
        contents = {name: Path(__file__).with_name(name).read_bytes() for name in native_sandbox.FILES}
        for name, raw in contents.items():
            self.assertEqual(hashlib.sha256(raw).hexdigest(), native_sandbox.FILES[name])
        original_lstat, original_fstat = Path.lstat, os.fstat

        def owned(metadata, uid=0):
            values = list(metadata)
            values[4] = uid
            return os.stat_result(values)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'policy'
            root.mkdir(mode=0o755)
            for name, raw in contents.items():
                (root / name).write_bytes(raw)
                (root / name).chmod(0o644)
            (root / native_sandbox.APPARMOR).write_bytes(contents['codex-apparmor-v1.profile'])
            (root / native_sandbox.APPARMOR).chmod(0o644)
            with mock.patch.object(native_sandbox, 'ROOT', root), \
                    mock.patch.object(native_sandbox, 'PROFILE', root / native_sandbox.APPARMOR), \
                    mock.patch.object(Path, 'lstat', lambda path: owned(original_lstat(path))), \
                    mock.patch.object(os, 'fstat', lambda fd: owned(original_fstat(fd))):
                self.assertEqual(native_sandbox.verify(), native_sandbox.FILES)
                selected = root / 'codex-seccomp-v1.json'
                selected.write_bytes(b'{}')
                with self.assertRaises(ValueError):
                    native_sandbox.verify()
                selected.write_bytes(contents[selected.name])
                selected.chmod(0o666)
                with self.assertRaises(ValueError):
                    native_sandbox.verify()
                selected.chmod(0o644)
                selected.unlink()
                selected.symlink_to(root / 'codex-apparmor-v1.profile')
                with self.assertRaises(OSError):
                    native_sandbox.verify()
            with mock.patch.object(native_sandbox, 'ROOT', root), \
                    mock.patch.object(native_sandbox, 'PROFILE', root / native_sandbox.APPARMOR):
                with self.assertRaises(ValueError):
                    native_sandbox.verify()

        self.addCleanup(native.configure, 'claude-haiku')
        native.configure('codex', codex_sandbox=True)
        options = ['no-new-privileges:true', 'apparmor=' + native_sandbox.APPARMOR,
                   'seccomp=' + json.dumps(json.loads(contents['codex-seccomp-v1.json']), separators=(',', ':'))]
        data = {'Config': {'User': '61043:61043'}, 'AppArmorProfile': native_sandbox.APPARMOR,
                'HostConfig': {'SecurityOpt': options, 'NetworkMode': 'bridge', 'PidMode': '',
                               'IpcMode': 'private', 'Privileged': False, 'ReadonlyRootfs': True,
                               'CapAdd': None, 'CapDrop': ['ALL']},
                'Mounts': [{'Type': 'volume', 'Destination': destination, 'Name': 'buzz-isolated-codex-' + suffix, 'RW': writable}
                           for destination, suffix, writable in [('/workspace', 'workspace', True),
                           ('/home/paul/buzz/nests/codex', 'workspace', True),
                           ('/home/paul', 'home', True), ('/home/paul/.ssh', 'ssh', False)]]}
        with mock.patch.object(native_sandbox, '_read_verified', return_value=contents), \
                mock.patch.object(pilot, 'docker', side_effect=lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout=json.dumps([data]))):
            native.audit('synthetic-daemon-record')
            for mutation in ['seccomp=unconfined', 'seccomp={}', 'apparmor=unconfined']:
                data['HostConfig']['SecurityOpt'] = options[:2] + [mutation]
                with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                    native.audit('synthetic-daemon-record')
            data['HostConfig']['SecurityOpt'] = options
            data['AppArmorProfile'] = 'unconfined'
            with self.assertRaises(ValueError):
                native.audit('synthetic-daemon-record')

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
        image = 'sha256:' + 'a' * 64
        for scenario in ['changed-settings', 'changed-policy', 'failed-start', 'replacement-still-running', 'policy-failed-start']:
            native.configure('codex' if scenario == 'policy-failed-start' else 'claude-haiku',
                             codex_sandbox=scenario == 'policy-failed-start')
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
                if scenario in ['changed-policy', 'policy-failed-start']:
                    record['sandbox_policy'] = dict(native_sandbox.FILES)
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
                        mock.patch.object(native_sandbox, 'verify', return_value=dict(native_sandbox.FILES)), \
                        mock.patch.object(pilot, 'WORKSPACE_ROOT', root), \
                        mock.patch.object(pilot, 'systemctl', side_effect=service_with_stop), \
                        mock.patch.object(pilot, 'docker', side_effect=docker):
                    with self.assertRaises((ValueError, RuntimeError)):
                        native.activate(image, env_file, snapshot)
                self.assertEqual((source / 'work.txt').read_text(), 'original work\n')
                restore = ('enable', '--now', pilot.ORIGINAL_SERVICE)
                if scenario in ['changed-settings', 'changed-policy']:
                    self.assertNotIn(('disable', '--now', pilot.ORIGINAL_SERVICE), calls)
                elif scenario in ['failed-start', 'policy-failed-start']:
                    self.assertLess(calls.index(('disable', '--now', pilot.NAME + '.service')), calls.index(restore))
                else:
                    self.assertNotIn(restore, calls)


@unittest.skipUnless(os.environ.get('NATIVE_IMAGE'), 'Set NATIVE_IMAGE for real native boundary negative controls')
class NativeBoundaryJourney(unittest.TestCase):
    def test_real_launch_and_mutated_authority(self):
        original = pilot.AGENT
        self.addCleanup(pilot.configure, original)
        native.configure(os.environ.get('NATIVE_AGENT', 'claude-haiku'),
                         codex_sandbox=os.environ.get('NATIVE_SANDBOX') == '1')
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
                        if native.CODEX_SANDBOX:
                            code = Path(__file__).with_name('codex_sandbox_probe.py').read_text()
                            result = pilot.docker(['exec', name, 'python3', '-c', code],
                                                  capture_output=True, text=True, timeout=40)
                            self.assertEqual(json.loads(result.stdout)['native_inner_sandbox'], 'passed')
                finally:
                    pilot.docker(['rm', '--force', name], capture_output=True)


class NativeProbeJourney(unittest.TestCase):
    def test_batched_adapter_updates_do_not_hide_the_completion_reply(self):
        import acp_probe
        import contextlib
        import io
        import sys

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            adapter = root / 'adapter.py'
            adapter.write_text('''import json,sys,subprocess
from pathlib import Path
for line in sys.stdin:
    request=json.loads(line)
    method=request['method']
    if method=='initialize':
        result={'protocolVersion':1,'agentInfo':{'name':'batched-adapter'}}
    elif method=='session/new':
        result={'sessionId':'local-session'}
    elif method=='session/prompt':
        p=Path('REPOS/batched-probe');p.mkdir(parents=True,exist_ok=True)
        (p/'result.txt').write_text('370\\n')
        subprocess.run(['git','init','-q',str(p)],check=True)
        if Path('wrong-index').exists():(p/'result.txt').write_text('wrong\\n')
        subprocess.run(['git','-C',str(p),'add','result.txt'],check=True)
        (p/'result.txt').write_text('370\\n')
        result={'stopReason':'end_turn'}
    else:raise RuntimeError('Unexpected method')
    update={'jsonrpc':'2.0','method':'session/update','params':{'update':{'sessionUpdate':'agent_message_chunk'}}}
    reply={'jsonrpc':'2.0','id':request['id'],'result':result}
    # Keep the connection open; both lines arrive in one write, as real
    # adapters may send their final update and RPC completion together.
    sys.stdout.write(json.dumps(update)+'\\n'+json.dumps(reply)+'\\n');sys.stdout.flush()
''')
            with contextlib.redirect_stdout(io.StringIO()) as output:
                acp_probe.probe([sys.executable,str(adapter)],str(root),None,2,'batched-probe')
            self.assertIn('"native_model_tools": "passed"',output.getvalue())
            (root / 'REPOS/batched-probe/result.txt').unlink()
            (root / 'wrong-index').touch()
            with contextlib.redirect_stdout(io.StringIO()), self.assertRaisesRegex(RuntimeError, 'staged Git bytes'):
                acp_probe.probe([sys.executable,str(adapter)],str(root),None,2,'batched-probe')

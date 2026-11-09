"""Exercise real provisioning decisions, failure recovery and private storage."""
from contextlib import ExitStack
import json
import os
import signal
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest import mock
import uuid

import pilot
import provision


BASE = 'sha256:' + 'a' * 64
DERIVED = 'sha256:' + 'b' * 64


def config():
    return {'User': '61032:61032', 'WorkingDir': '/workspace',
            'Entrypoint': ['python3', '/opt/buzz/contained.py'],
            'Env': ['PATH=/opt/buzz/bin:/usr/bin:/bin', 'HOME=/workspace', 'BUZZ_NEST=/workspace',
                    'BUZZ_ACP_AGENT_COMMAND=/opt/buzz/bin/buzz-agent', 'BUZZ_ACP_AGENT_ARGS=',
                    'BUZZ_ACP_MCP_COMMAND=/opt/buzz/bin/buzz-dev-mcp',
                    'BUZZ_ACP_BASE_PROMPT_FILE=/opt/buzz/base-prompt.md']}


class Provisioning(unittest.TestCase):
    def fixture(self, home, name='new-worker'):
        source = home / ('buzz-acp-' + name + '.env')
        source.write_text('BUZZ_PRIVATE_KEY=synthetic-test-only\nBUZZ_RELAY_URL=wss://relay.invalid\n'
                          'BUZZ_ACP_AGENT_COMMAND=/host/path/buzz-agent\nBUZZ_AGENT_PROVIDER=openai\n'
                          'OPENAI_COMPAT_API_KEY=synthetic-test-only\n'
                          'OPENAI_COMPAT_BASE_URL=https://provider.invalid/v1\n'
                          'OPENAI_COMPAT_MODEL=synthetic-model\n')
        source.chmod(0o600)
        return source

    def service(self, *args, **kwargs):
        value = ''
        if args[0] == 'show':
            value = {'MainPID': '0', 'ActiveState': 'inactive', 'UnitFileState': 'disabled'}[
                args[2].split('=', 1)[1]]
        return subprocess.CompletedProcess(args, 0, stdout=value)

    def docker(self, args, **kwargs):
        value = ''
        if '{{json .Config}}' in args:
            value = json.dumps(config())
        elif '{{.Config.User}}' in args:
            value = f'{pilot.UID}:{pilot.UID}'
        elif '{{.State.Running}}' in args:
            value = 'true'
        elif '{{.Id}}' in args:
            value = BASE
        elif args[0] == 'build':
            Path(args[args.index('--iidfile') + 1]).write_text(DERIVED)
            dockerfile = Path(args[-1], 'Dockerfile').read_text()
            self.assertIn('FROM buzz-provision-base:' + BASE.split(':')[1], dockerfile)
            self.assertNotIn('COPY', dockerfile)
            self.assertEqual(set(p.name for p in Path(args[-1]).iterdir()), {'Dockerfile', 'image-id'})
        return subprocess.CompletedProcess(args, 0, stdout=value)

    def setup_context(self, home):
        self.addCleanup(pilot.configure_profile, pilot.AGENT, pilot.UID, pilot.DISPLAY_NAME)
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(mock.patch.object(Path, 'home', return_value=home))
        stack.enter_context(mock.patch.object(provision, 'ROOT', home / 'profiles'))
        stack.enter_context(mock.patch.object(pilot, 'WORKSPACE_ROOT', home / 'nests'))
        return stack

    def test_locked_reservations_distinct_uids_and_existing_name_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            stack = self.setup_context(home)
            system = stack.enter_context(mock.patch.object(pilot, 'systemctl', side_effect=self.service))
            docker = stack.enter_context(mock.patch.object(pilot, 'docker', side_effect=self.docker))
            for name in ['new-worker', 'other-worker']:
                provision.provision(pilot, name, self.fixture(home, name), BASE)
                record = provision.load_profile(name)
                pilot.configure(name)
                self.assertEqual(pilot.UID, record['uid'])
                args = pilot.runtime_args(DERIVED)
                self.assertEqual(args.count('--mount'), 1)
                self.assertIn('type=volume,source=buzz-isolated-' + name + '-workspace,target=/workspace', args)
                unit = home / '.config/systemd/user' / ('buzz-isolated-' + name + '.service')
                self.assertIn('run --image ' + DERIVED, unit.read_text())
                self.assertIn('UnsetEnvironment=', unit.read_text())
            self.assertNotEqual(provision.load_profile('new-worker')['uid'], provision.load_profile('other-worker')['uid'])
            with self.assertRaises(ValueError):
                provision.provision(pilot, 'new-worker', home / 'buzz-acp-new-worker.env', BASE)
            self.assertFalse(any('buzz-acp@' in str(call) and call.args[0] != 'show'
                                 for call in system.call_args_list))
            self.assertTrue(any(call.args[0][0] == 'run' for call in docker.call_args_list))

    def test_failure_preserves_reservation_and_never_starts_host(self):
        for failure in ['build', 'run', 'enable']:
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                home = Path(directory)
                stack = self.setup_context(home)
                calls = []

                def system(*args, **kwargs):
                    calls.append(args)
                    if args[0] == failure:
                        raise subprocess.CalledProcessError(1, ['systemctl'])
                    return self.service(*args)

                def docker(args, **kwargs):
                    if args[0] == failure:
                        raise subprocess.CalledProcessError(1, ['docker'])
                    return self.docker(args, **kwargs)

                with mock.patch.object(pilot, 'systemctl', side_effect=system), \
                        mock.patch.object(pilot, 'docker', side_effect=docker):
                    with self.assertRaises(subprocess.CalledProcessError):
                        provision.provision(pilot, 'new-worker', self.fixture(home), BASE)
                record = json.loads((home / 'profiles/new-worker.json').read_text())
                self.assertEqual(record['state'], 'failed')
                with self.assertRaises(ValueError):
                    provision.load_profile('new-worker')
                self.assertFalse(any(call[0] == 'enable' and 'buzz-acp@' in str(call) for call in calls))
                if failure == 'enable':
                    self.assertIn(('disable', '--now', 'buzz-isolated-new-worker.service'), calls)
                stack.close()

    def test_crash_temp_cannot_block_record_updates_and_cleanup_preserves_first_error(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            stack = self.setup_context(home)
            provision.ROOT.mkdir()
            (provision.ROOT / 'new-worker.tmp').write_text('interrupted old write')
            original = subprocess.CalledProcessError(17, ['synthetic-enable-error'])

            def system(*args, **kwargs):
                if args[0] == 'enable':
                    raise original
                if args[0] == 'disable':
                    raise subprocess.CalledProcessError(18, ['synthetic-stop-error'])
                return self.service(*args)

            with mock.patch.object(pilot, 'systemctl', side_effect=system), \
                    mock.patch.object(pilot, 'docker', side_effect=self.docker):
                with self.assertRaises(subprocess.CalledProcessError) as caught:
                    provision.provision(pilot, 'new-worker', self.fixture(home), BASE)
            self.assertIs(caught.exception, original)
            self.assertEqual(json.loads((provision.ROOT / 'new-worker.json').read_text())['state'], 'failed')
            self.assertIn('Replacement stop needs operator inspection', original.__notes__[0])
            self.assertEqual((provision.ROOT / 'new-worker.tmp').read_text(), 'interrupted old write')
            stack.close()

    def test_whole_operation_deadline_restores_process_alarm(self):
        before = signal.getsignal(signal.SIGALRM)
        with self.assertRaises(TimeoutError):
            with provision.operation_deadline(0.01):
                time.sleep(0.1)
        self.assertEqual(signal.getsignal(signal.SIGALRM), before)
        self.assertEqual(signal.getitimer(signal.ITIMER_REAL), (0, 0))

    def test_collisions_and_native_adapters_refused_before_reservation(self):
        for collision in ['native', 'workspace', 'unit', 'volume', 'host']:
            with self.subTest(collision=collision), tempfile.TemporaryDirectory() as directory:
                home = Path(directory)
                stack = self.setup_context(home)
                source = self.fixture(home)
                if collision == 'native':
                    source.write_text(source.read_text().replace('/host/path/buzz-agent', 'codex-acp'))
                elif collision == 'workspace':
                    (home / 'nests/new-worker').mkdir(parents=True)
                    (home / 'nests/new-worker/saved').write_text('retain')
                elif collision == 'unit':
                    unit = home / '.config/systemd/user/buzz-isolated-new-worker.service'
                    unit.parent.mkdir(parents=True)
                    unit.symlink_to(home / 'missing')

                def docker(args, **kwargs):
                    if collision == 'volume' and args[:2] == ['volume', 'ls']:
                        return subprocess.CompletedProcess(args, 0, stdout='existing-volume')
                    return self.docker(args, **kwargs)

                def system(*args):
                    if collision == 'host' and '--property=MainPID' in args:
                        return subprocess.CompletedProcess(args, 0, stdout='1234')
                    return self.service(*args)

                with mock.patch.object(pilot, 'systemctl', side_effect=system), \
                        mock.patch.object(pilot, 'docker', side_effect=docker):
                    with self.assertRaises(ValueError):
                        provision.provision(pilot, 'new-worker', source, BASE)
                self.assertFalse((home / 'profiles/new-worker.json').exists())
                stack.close()

    def test_unsafe_base_rejected_and_runtime_cannot_change_recorded_image(self):
        for change in ['secret', 'command', 'volumes', 'desktop']:
            value = config()
            if change == 'secret':
                value['Env'].append('BUZZ_PRIVATE_KEY=synthetic')
            elif change == 'command':
                value['Entrypoint'] = ['sh']
            elif change == 'volumes':
                value['Volumes'] = {'/home': {}}
            else:
                value['Labels'] = {'buzz.isolation.desktop': 'fizz'}
            with mock.patch.object(pilot, 'docker', return_value=subprocess.CompletedProcess([], 0, stdout=json.dumps(value))):
                with self.assertRaises(ValueError):
                    provision.inspect_base(pilot, BASE)
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            self.setup_context(home)
            provision.ROOT.mkdir()
            provision.write_record(provision.ROOT / 'new-worker.json', {
                'agent': 'new-worker', 'uid': 62000, 'state': 'ready', 'image': DERIVED})
            pilot.configure('new-worker')
            with mock.patch.object(pilot, 'docker') as docker:
                with self.assertRaises(ValueError):
                    pilot.start(BASE)
                docker.assert_not_called()

    def test_wrong_build_identity_or_pin_stays_failed_before_service_creation(self):
        for field in ['{{.Id}}', '{{.Config.User}}']:
            with self.subTest(field=field), tempfile.TemporaryDirectory() as directory:
                home = Path(directory)
                stack = self.setup_context(home)

                def docker(args, **kwargs):
                    if field in args:
                        return subprocess.CompletedProcess(args, 0, stdout='changed')
                    return self.docker(args, **kwargs)

                with mock.patch.object(pilot, 'systemctl', side_effect=self.service) as system, \
                        mock.patch.object(pilot, 'docker', side_effect=docker):
                    with self.assertRaises(ValueError):
                        provision.provision(pilot, 'new-worker', self.fixture(home), BASE)
                self.assertEqual(json.loads((home / 'profiles/new-worker.json').read_text())['state'], 'failed')
                self.assertTrue(all(call.args[0] == 'show' for call in system.call_args_list))
                stack.close()


@unittest.skipUnless(os.environ.get('PILOT_IMAGE'), 'Set PILOT_IMAGE for real fresh-worker storage')
class ProvisionedStorageJourney(unittest.TestCase):
    fixture = Provisioning.fixture
    service = Provisioning.service
    setup_context = Provisioning.setup_context

    def test_real_named_volume_collision_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            self.setup_context(home)
            name = 'provision-collision-' + uuid.uuid4().hex[:12]
            volume = 'buzz-isolated-' + name + '-workspace'
            pilot.docker(['volume', 'create', volume], capture_output=True)
            self.addCleanup(subprocess.run, ['docker', 'volume', 'rm', volume],
                            capture_output=True, timeout=30)
            with mock.patch.object(pilot, 'systemctl', side_effect=self.service):
                with self.assertRaisesRegex(ValueError, 'Existing container or workspace volume'):
                    provision.provision(pilot, name, self.fixture(home, name), os.environ['PILOT_IMAGE'])
            self.assertFalse((provision.ROOT / (name + '.json')).exists())

    def test_real_uid_derivative_and_fresh_private_volume(self):
        # Exercise the production provisioner with Docker, while substituting
        # systemd only: this test never starts a real agent or uses credentials.
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            self.setup_context(home)
            name = 'provision-test-' + uuid.uuid4().hex[:12]
            volume = 'buzz-isolated-' + name + '-workspace'
            self.addCleanup(subprocess.run, ['docker', 'volume', 'rm', volume],
                            capture_output=True, timeout=30)
            real_docker = pilot.docker

            def docker(args, **kwargs):
                if '{{.State.Running}}' in args:
                    return subprocess.CompletedProcess(args, 0, stdout='true')
                try:
                    return real_docker(args, **kwargs)
                except subprocess.CalledProcessError as error:
                    if args[0] == 'build':
                        print((error.stderr or '')[-4000:])
                    raise

            with mock.patch.object(pilot, 'systemctl', side_effect=self.service), \
                    mock.patch.object(pilot, 'docker', side_effect=docker):
                provision.provision(pilot, name, self.fixture(home, name), os.environ['PILOT_IMAGE'])
            record = provision.load_profile(name)
            code = ('from pathlib import Path; import os,subprocess; p=Path("/workspace/REPOS/probe"); p.mkdir(parents=True); '
                    'p.joinpath("result.txt").write_bytes(b"370\\n"); '
                    'subprocess.run(["git","init","-q",str(p)],check=True); '
                    'subprocess.run(["git","-C",str(p),"add","result.txt"],check=True); '
                    'print(os.getuid(),p.joinpath("result.txt").stat().st_uid)')
            args = pilot.runtime_args(record['image'], name=pilot.NAME + '-verify', entrypoint='python3', network='none')
            result = real_docker([*args, record['image'], '-c', code], capture_output=True, text=True)
            self.assertEqual(result.stdout.strip(), f"{record['uid']} {record['uid']}")
            # Inspect the stored bytes independently with the original base.
            read = real_docker(['run', '--rm', '--network', 'none', '--read-only', '--cap-drop', 'ALL',
                                '--user', str(record['uid']), '--mount',
                                f'type=volume,source={volume},target=/workspace,readonly,volume-nocopy',
                                '--entrypoint', 'python3', os.environ['PILOT_IMAGE'], '-c',
                                'from pathlib import Path; import subprocess; '
                                'raw=Path("/workspace/REPOS/probe/result.txt").read_bytes(); '
                                'assert raw==subprocess.check_output(["git","-C","/workspace/REPOS/probe","show",":result.txt"]); '
                                'print(raw.hex())'],
                               capture_output=True, text=True)
            self.assertEqual(read.stdout.strip(), '3337300a')

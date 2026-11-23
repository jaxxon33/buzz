"""Fresh agy state and lifecycle boundaries, including the real unauthenticated CLI."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock
import uuid

import agy_profile
import native_build
import pilot
import provision
import test_provision

BASE, DERIVED = test_provision.BASE, test_provision.DERIVED

MODEL = 'gemini-4-neon-medium'


def agy_config(label='agy'):
    value = test_provision.config()
    value['User'] = '61050:61050'
    value['Labels'] = {'buzz.isolation.native': label}
    value['Env'] = [item.replace('HOME=/workspace', 'HOME=/home/paul').replace(
        'BUZZ_ACP_AGENT_COMMAND=/opt/buzz/bin/buzz-agent',
        'BUZZ_ACP_AGENT_COMMAND=/opt/buzz/bin/native-adapter') for item in value['Env']]
    return value


class AgyProfile(unittest.TestCase):
    setup_context = test_provision.Provisioning.setup_context
    service = test_provision.Provisioning.service
    label = 'agy'

    def fixture(self, home, name='new-agy'):
        source = home / ('buzz-acp-' + name + '.env')
        source.write_text('BUZZ_PRIVATE_KEY=synthetic-agy-key\nBUZZ_RELAY_URL=wss://relay.invalid\n'
                          'BUZZ_ACP_AGENT_COMMAND=agy\nBUZZ_ACP_AGENT_ARGS=acp\nBUZZ_ACP_MODEL=' + MODEL + '\n')
        source.chmod(0o600)
        return source

    def docker(self, args, **kwargs):
        value = ''
        if '{{json .Config}}' in args:
            value = json.dumps(agy_config(self.label))
        elif '{{.Id}}' in args:
            value = BASE
        elif '{{.Config.User}}' in args:
            value = f'{pilot.UID}:{pilot.UID}'
        elif args[0] == 'build':
            context = Path(args[-1])
            self.assertFalse((context / 'managed-settings.json').exists())
            self.assertIn('native-worker', (context / 'Dockerfile').read_text())
            Path(args[args.index('--iidfile') + 1]).write_text(DERIVED)
        elif '{{.State.Running}}' in args:
            value = 'true'
        return subprocess.CompletedProcess(args, 0, stdout=value)

    def prepare(self, home):
        source = self.fixture(home)
        with mock.patch.object(pilot, 'systemctl', side_effect=self.service), \
                mock.patch.object(pilot, 'docker', side_effect=self.docker):
            provision.provision(pilot, 'new-agy', source, BASE)
        return provision.load_profile('new-agy', states=('awaiting-login',))

    def test_prepare_creates_private_login_home_without_starting_or_copying_host_auth(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            self.setup_context(home)
            with mock.patch.object(pilot, 'systemctl', side_effect=self.service) as service, \
                    mock.patch.object(pilot, 'docker', side_effect=self.docker) as docker:
                provision.provision(pilot, 'new-agy', self.fixture(home), BASE)
            record = provision.load_profile('new-agy', states=('awaiting-login',))
            self.assertEqual(record['family'], 'agy')
            self.assertEqual(record['model'], MODEL)
            self.assertFalse(any(call.args[0] == 'enable' for call in service.call_args_list))
            created = [call.args[0][-1] for call in docker.call_args_list if call.args[0][:2] == ['volume', 'create']]
            self.assertEqual(created, ['buzz-isolated-new-agy-workspace', 'buzz-isolated-new-agy-home'])
            args = agy_profile.runtime_args(pilot, DERIVED)
            mounts = [item for item in args if item.startswith('type=volume,')]
            self.assertEqual(mounts, [
                'type=volume,source=buzz-isolated-new-agy-workspace,target=/workspace',
                'type=volume,source=buzz-isolated-new-agy-home,target=/home/paul'])
            self.assertNotIn('type=bind', str(args))
            self.assertNotIn('ssh', str(args))
            self.assertNotIn('dbus', str(args).lower())
            self.assertNotIn('synthetic-agy-key', str(docker.call_args_list))
            unit = (home / '.config/systemd/user/buzz-isolated-new-agy.service').read_text()
            self.assertIn('BUZZ_ACP_MODEL=', unit)
            self.assertNotIn('GEMINI_API_KEY', unit)
            with self.assertRaises(ValueError):
                pilot.start(DERIVED)

    def test_claude_image_cannot_back_an_agy_profile(self):
        self.label = 'claude'
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            self.setup_context(home)
            with mock.patch.object(pilot, 'systemctl', side_effect=self.service), \
                    mock.patch.object(pilot, 'docker', side_effect=self.docker):
                with self.assertRaisesRegex(ValueError, 'agy image'):
                    provision.provision(pilot, 'new-agy', self.fixture(home), BASE)
            self.assertFalse((home / 'profiles/new-agy.json').exists())

    def test_rejects_api_keys_vertex_keyring_and_model_aliases(self):
        with tempfile.TemporaryDirectory() as directory:
            source = self.fixture(Path(directory))
            original = source.read_text()
            for key in agy_profile.HOST_AUTH:
                source.write_text(original + key + '=must-not-be-forwarded\n')
                with self.subTest(key=key), self.assertRaises(ValueError):
                    provision.read_environment(source)
            for model in ['neon', 'auto', 'claude-fable-5-2', 'gemini']:
                source.write_text(original.replace(MODEL, model))
                with self.subTest(model=model), self.assertRaises(ValueError):
                    provision.read_environment(source)
            source.write_text(original.replace('BUZZ_ACP_AGENT_ARGS=acp', 'BUZZ_ACP_AGENT_ARGS=--dangerously-skip-permissions'))
            with self.assertRaises(ValueError):
                provision.read_environment(source)
            source.write_text(original)
            self.assertEqual(provision.read_environment(source)['BUZZ_ACP_MODEL'], MODEL)
        self.assertFalse(agy_profile.HOST_AUTH.intersection(agy_profile.forwarded(pilot)))
        self.assertIn('BUZZ_ACP_SESSION_POLICY', agy_profile.forwarded(pilot))

    def test_worker_drops_empty_defaults_but_keeps_declared_messaging(self):
        environment = {'BUZZ_PRIVATE_KEY': 'synthetic', 'BUZZ_RELAY_URL': 'wss://relay.invalid',
                       'BUZZ_ACP_MODEL': MODEL, 'BUZZ_AUTH_TAG': '',
                       'BUZZ_ACP_RESPOND_TO_ALLOWLIST': 'synthetic-owner'}
        with mock.patch.dict(os.environ, environment, clear=True), \
                mock.patch.object(provision, 'load_profile', return_value={
                    'family': 'agy', 'image': DERIVED, 'model': MODEL}), \
                mock.patch.object(agy_profile.socket, 'getaddrinfo', return_value=[
                    (2, 1, 6, '', ('127.0.0.1', 443))]), \
                mock.patch.object(pilot, 'docker', return_value=subprocess.CompletedProcess([], 0, stdout='127.0.0.1')), \
                mock.patch.object(agy_profile.subprocess, 'call', return_value=0) as launch:
            self.assertEqual(agy_profile.start(pilot, DERIVED), 0)
        args = launch.call_args.args[0]
        self.assertIn('BUZZ_ACP_RESPOND_TO_ALLOWLIST', args)
        self.assertNotIn('BUZZ_AUTH_TAG', args)
        with mock.patch.dict(os.environ, dict(environment, BUZZ_ACP_MODEL='gemini-4-neon-high'), clear=True), \
                mock.patch.object(provision, 'load_profile', return_value={
                    'family': 'agy', 'image': DERIVED, 'model': MODEL}):
            with self.assertRaisesRegex(ValueError, 'differs'):
                agy_profile.start(pilot, DERIVED)

    def test_missing_login_or_failed_model_check_never_enables_service(self):
        for failure in ['auth_status', 'model_check']:
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                home = Path(directory)
                stack = self.setup_context(home)
                self.prepare(home)
                with mock.patch.object(pilot, 'systemctl', side_effect=self.service) as service, \
                        mock.patch.object(pilot, 'docker', side_effect=self.docker), \
                        mock.patch.object(agy_profile, 'auth_status'), \
                        mock.patch.object(agy_profile, 'model_check'), \
                        mock.patch.object(agy_profile, failure, side_effect=ValueError('not verified')):
                    with self.assertRaises(ValueError):
                        agy_profile.activate(pilot, 'new-agy')
                self.assertFalse(any(call.args[0] == 'enable' for call in service.call_args_list))
                self.assertEqual(provision.load_profile('new-agy', states=('awaiting-login',))['state'], 'awaiting-login')
                stack.close()

    def test_activation_failure_stops_only_replacement_and_retains_pending_login(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            self.setup_context(home)
            self.prepare(home)
            calls = []

            def service(*args, **kwargs):
                calls.append(args)
                if args[0] == 'enable':
                    raise subprocess.CalledProcessError(1, ['synthetic-start'])
                return self.service(*args)

            with mock.patch.object(pilot, 'systemctl', side_effect=service), \
                    mock.patch.object(pilot, 'docker', side_effect=self.docker), \
                    mock.patch.object(agy_profile, 'auth_status'), \
                    mock.patch.object(agy_profile, 'model_check'):
                with self.assertRaises(subprocess.CalledProcessError):
                    agy_profile.activate(pilot, 'new-agy')
            self.assertIn(('disable', '--now', 'buzz-isolated-new-agy.service'), calls)
            self.assertFalse(any('buzz-acp@' in str(args) for args in calls))
            self.assertEqual(provision.load_profile('new-agy', states=('awaiting-login',))['state'], 'awaiting-login')

    def test_status_requires_authenticated_session_offering_the_model(self):
        record = {'image': DERIVED, 'model': MODEL}
        for row in [{'logged_in': False, 'error_code': -32000, 'models': []},
                    {'logged_in': True, 'models': ['gemini-4-neon-low']}]:
            status = subprocess.CompletedProcess([], 0, stdout=json.dumps(row) + '\n')
            with self.subTest(row=row), mock.patch.object(agy_profile, 'helper', return_value=status):
                with self.assertRaises(ValueError):
                    agy_profile.auth_status(pilot, record)
        with mock.patch.object(agy_profile, 'helper', return_value=subprocess.CompletedProcess([], 0, stdout='')):
            with self.assertRaisesRegex(ValueError, 'not signed in'):
                agy_profile.auth_status(pilot, record)
        status = subprocess.CompletedProcess([], 0, stdout=json.dumps({'logged_in': True, 'models': [MODEL]}))
        with mock.patch.object(agy_profile, 'helper', return_value=status):
            agy_profile.auth_status(pilot, record)

    def test_activation_checks_login_model_and_actual_launch_before_success(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            self.setup_context(home)
            self.prepare(home)
            steps = []

            def service(*args, **kwargs):
                if args[0] in {'enable', 'is-active'}:
                    steps.append(args[0])
                return self.service(*args)

            with mock.patch.object(pilot, 'systemctl', side_effect=service), \
                    mock.patch.object(pilot, 'docker', side_effect=self.docker), \
                    mock.patch.object(agy_profile, 'auth_status', side_effect=lambda *_: steps.append('auth')), \
                    mock.patch.object(agy_profile, 'model_check', side_effect=lambda *_: steps.append('model')), \
                    mock.patch.object(agy_profile, 'audit', side_effect=lambda *_: steps.append('audit')):
                agy_profile.activate(pilot, 'new-agy')
            self.assertEqual(steps, ['auth', 'model', 'enable', 'audit', 'is-active'])
            self.assertEqual(provision.load_profile('new-agy')['family'], 'agy')

    def test_changed_pending_settings_cannot_activate_without_reverification(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            self.setup_context(home)
            self.prepare(home)
            source = home / 'buzz-acp-new-agy.env'
            source.write_text(source.read_text().replace(MODEL, 'gemini-4-neon-high'))
            with mock.patch.object(pilot, 'systemctl', side_effect=self.service) as service, \
                    mock.patch.object(pilot, 'docker', side_effect=self.docker), \
                    mock.patch.object(agy_profile, 'auth_status') as auth:
                with self.assertRaisesRegex(ValueError, 'settings changed'):
                    agy_profile.activate(pilot, 'new-agy')
            auth.assert_not_called()
            self.assertFalse(any(call.args[0] == 'enable' for call in service.call_args_list))

    def test_claude_commands_refuse_agy_profiles_and_vice_versa(self):
        import claude_profile
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            self.setup_context(home)
            self.prepare(home)
            with mock.patch.object(pilot, 'systemctl', side_effect=self.service), \
                    mock.patch.object(pilot, 'docker', side_effect=self.docker):
                with self.assertRaisesRegex(ValueError, 'pending Claude'):
                    claude_profile.activate(pilot, 'new-agy')

    def test_login_requires_owner_terminal_and_helpers_preserve_existing_names(self):
        with mock.patch.object(agy_profile.os, 'isatty', return_value=False), \
                mock.patch.object(pilot, 'docker') as docker:
            with self.assertRaisesRegex(ValueError, 'interactive terminal'):
                agy_profile.login(pilot, 'new-agy')
            docker.assert_not_called()
        with mock.patch.object(pilot, 'docker', return_value=subprocess.CompletedProcess([], 0, stdout='existing')) as docker:
            with self.assertRaisesRegex(ValueError, 'Existing agy helper'):
                agy_profile.helper(pilot, {'image': DERIVED}, 'reserved-helper', ['python3', '-c', 'pass'])
            self.assertEqual(len(docker.call_args_list), 1)

    def test_timed_out_helper_removes_its_exact_container(self):
        self.addCleanup(pilot.configure_profile, pilot.AGENT, pilot.UID, pilot.DISPLAY_NAME)
        pilot.configure_profile('new-agy', 62000, 'new-agy')
        running = False
        removed = []

        def docker(args, **kwargs):
            nonlocal running
            if args[0] == 'run':
                running = True
                raise subprocess.TimeoutExpired('docker', 30)
            if args[:2] == ['ps', '-aq']:
                return subprocess.CompletedProcess(args, 0, stdout='own-helper' if running else '')
            if args[:2] == ['rm', '--force']:
                removed.append(args[-1])
                running = False
            return subprocess.CompletedProcess(args, 0, stdout='')

        with mock.patch.object(pilot, 'docker', side_effect=docker):
            with self.assertRaises(subprocess.TimeoutExpired):
                agy_profile.helper(pilot, {'image': DERIVED}, 'new-agy-check', ['python3', '-c', 'pass'])
        self.assertEqual(removed, ['new-agy-check'])
        self.assertFalse(running)

    def test_image_build_pins_agy_release_and_uses_no_node_or_host_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            local = root / 'local'
            (local / 'bin').mkdir(parents=True)
            for name in ['buzz-acp', 'buzz', 'buzz-dev-mcp']:
                (local / 'bin' / name).write_bytes(b'synthetic ' + name.encode())
            agy = root / 'agy-release' / 'agy'
            agy.parent.mkdir()
            agy.write_bytes(b'synthetic agy')
            prompt = root / 'prompt.md'
            prompt.write_text('shared instructions')
            digest = __import__('hashlib').sha512(b'synthetic agy').hexdigest()
            real_path = Path

            def fake_path(value, *rest):
                if str(value) == '/home/paul/.local':
                    return local
                return real_path(value, *rest)

            docker = mock.Mock(return_value=subprocess.CompletedProcess([], 0, stdout=BASE))
            with mock.patch.object(native_build, 'AGY', agy), \
                    mock.patch.object(native_build, 'Path', side_effect=fake_path), \
                    mock.patch.object(pilot, 'docker', docker):
                with self.assertRaisesRegex(ValueError, 'SHA-512'):
                    native_build.build(root / 'bad', BASE, None, 'agy', 61050, prompt, 'f' * 128)
                native_build.build(root / 'ctx', BASE, None, 'agy', 61050, prompt, digest)
            context = root / 'ctx'
            dockerfile = (context / 'Dockerfile').read_text()
            self.assertNotIn('node', dockerfile.replace('native', ''))
            self.assertIn('LABEL buzz.isolation.native="agy"', dockerfile)
            self.assertEqual((context / 'runtime/agy').read_bytes(), b'synthetic agy')
            self.assertIn('agy acp --disable-telemetry', (context / 'bin/native-adapter').read_text())
            files = sorted(str(p.relative_to(context)) for p in context.rglob('*') if p.is_file())
            self.assertEqual(files, ['Dockerfile', 'base-prompt.md', 'bin/buzz', 'bin/buzz-acp', 'bin/buzz-dev-mcp',
                                     'bin/native-adapter', 'contained.py', 'manifest.json', 'relay_tunnel.py',
                                     'runtime/agy'])


@unittest.skipUnless(os.environ.get('AGY_NEW_IMAGE'), 'Set AGY_NEW_IMAGE for clean agy storage/login journey')
class FreshAgyJourney(unittest.TestCase):
    setup_context = test_provision.Provisioning.setup_context
    service = test_provision.Provisioning.service
    fixture = AgyProfile.fixture

    def test_real_private_home_uid_and_unauthenticated_cli(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            self.setup_context(home)
            name = 'agy-test-' + uuid.uuid4().hex[:12]
            prefix = 'buzz-isolated-' + name
            for suffix in ['-workspace', '-home']:
                self.addCleanup(subprocess.run, ['docker', 'volume', 'rm', prefix + suffix],
                                capture_output=True, timeout=30)
            with mock.patch.object(pilot, 'systemctl', side_effect=self.service):
                provision.provision(pilot, name, self.fixture(home, name), os.environ['AGY_NEW_IMAGE'])
            record = provision.load_profile(name, states=('awaiting-login',))
            with self.assertRaisesRegex(ValueError, 'not signed in'):
                agy_profile.auth_status(pilot, record)
            code = ('import os,socket,subprocess; from pathlib import Path; '
                    f'assert os.getuid()=={record["uid"]}; '
                    'assert not Path("/run/host").exists(); assert not Path("/var/run/docker.sock").exists(); '
                    'assert not Path("/run/dbus").exists() and "DBUS_SESSION_BUS_ADDRESS" not in os.environ; '
                    'assert not [k for k in os.environ if k.startswith(("GEMINI_","GOOGLE_"))]; '
                    'socket.getaddrinfo("oauth2.googleapis.com", 443); '
                    'p=Path("/workspace/REPOS/probe"); p.mkdir(parents=True); '
                    'p.joinpath("result.txt").write_bytes(b"370\\n"); '
                    'subprocess.run(["git","init","-q",str(p)],check=True); '
                    'subprocess.run(["git","-C",str(p),"add","result.txt"],check=True); '
                    'Path("/home/paul/own-home.txt").write_text("private"); '
                    'print("fresh_agy_boundary_passed")')
            result = agy_profile.helper(pilot, record, prefix + '-probe', ['python3', '-'], network='bridge',
                                        input=code, capture_output=True, text=True, timeout=30)
            self.assertIn('fresh_agy_boundary_passed', result.stdout)
            independent = ('from pathlib import Path; import subprocess; p=Path("/workspace/REPOS/probe"); '
                           f'assert p.joinpath("result.txt").stat().st_uid=={record["uid"]}; '
                           'assert Path("/home/paul/own-home.txt").read_text()=="private"; '
                           'assert subprocess.check_output(["git","-C",str(p),"show",":result.txt"])==b"370\\n"')
            agy_profile.helper(pilot, record, prefix + '-independent', ['python3', '-c', independent],
                               readonly=True, capture_output=True, timeout=30)
            name = prefix + '-audit'
            args = agy_profile.runtime_args(pilot, record['image'], name=name,
                                            entrypoint='sleep', network='none')
            pilot.docker([*args, '--detach', record['image'], '60'], capture_output=True)
            try:
                agy_profile.audit(pilot, name, record['image'], network='none')
            finally:
                agy_profile.remove_helper(pilot, name)

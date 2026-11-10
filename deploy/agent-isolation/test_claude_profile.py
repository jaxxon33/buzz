"""Fresh Claude state and lifecycle boundaries, including real unauthenticated CLI."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock
import uuid

import claude_profile
import pilot
import provision
import test_provision

BASE, DERIVED = test_provision.BASE, test_provision.DERIVED

MODEL = 'claude-fable-5-2'


def claude_config():
    value = test_provision.config()
    value['User'] = '61042:61042'
    value['Labels'] = {'buzz.isolation.native': 'claude'}
    value['Env'] = [item.replace('HOME=/workspace', 'HOME=/home/paul').replace(
        'BUZZ_ACP_AGENT_COMMAND=/opt/buzz/bin/buzz-agent',
        'BUZZ_ACP_AGENT_COMMAND=/opt/buzz/bin/native-adapter') for item in value['Env']]
    return value


class ClaudeProfile(unittest.TestCase):
    setup_context = test_provision.Provisioning.setup_context
    service = test_provision.Provisioning.service

    def test_worker_drops_empty_defaults_but_keeps_declared_messaging(self):
        environment = {'BUZZ_PRIVATE_KEY': 'synthetic', 'BUZZ_RELAY_URL': 'wss://relay.invalid',
                       'BUZZ_ACP_MODEL': MODEL, 'BUZZ_AUTH_TAG': '',
                       'BUZZ_ACP_MAX_TURNS_PER_SESSION': '',
                       'BUZZ_ACP_RESPOND_TO_ALLOWLIST': 'synthetic-owner'}
        with mock.patch.dict(os.environ, environment, clear=True), \
                mock.patch.object(provision, 'load_profile', return_value={
                    'family': 'claude', 'image': DERIVED, 'model': MODEL}), \
                mock.patch.object(claude_profile, 'public_routes', return_value=[]), \
                mock.patch.object(claude_profile.socket, 'getaddrinfo', return_value=[
                    (2, 1, 6, '', ('127.0.0.1', 443))]), \
                mock.patch.object(pilot, 'docker', return_value=subprocess.CompletedProcess([], 0, stdout='127.0.0.1')), \
                mock.patch.object(claude_profile.subprocess, 'call', return_value=0) as launch:
            self.assertEqual(claude_profile.start(pilot, DERIVED), 0)
        args = launch.call_args.args[0]
        self.assertIn('BUZZ_ACP_RESPOND_TO_ALLOWLIST', args)
        self.assertNotIn('BUZZ_AUTH_TAG', args)
        self.assertNotIn('BUZZ_ACP_MAX_TURNS_PER_SESSION', args)

    def fixture(self, home, name='new-claude'):
        source = home / ('buzz-acp-' + name + '.env')
        source.write_text('BUZZ_PRIVATE_KEY=synthetic-claude-key\nBUZZ_RELAY_URL=wss://relay.invalid\n'
                          'BUZZ_ACP_AGENT_COMMAND=claude-agent-acp\nBUZZ_ACP_MODEL=' + MODEL + '\n')
        source.chmod(0o600)
        return source

    def docker(self, args, **kwargs):
        value = ''
        if '{{json .Config}}' in args:
            value = json.dumps(claude_config())
        elif '{{.Id}}' in args:
            value = BASE
        elif '{{.Config.User}}' in args:
            value = f'{pilot.UID}:{pilot.UID}'
        elif args[0] == 'build':
            context = Path(args[-1])
            policy = json.loads((context / 'managed-settings.json').read_text())
            self.assertEqual(policy, claude_profile.model_policy(MODEL))
            self.assertIn('native-worker', (context / 'Dockerfile').read_text())
            Path(args[args.index('--iidfile') + 1]).write_text(DERIVED)
        elif '{{.State.Running}}' in args:
            value = 'true'
        return subprocess.CompletedProcess(args, 0, stdout=value)

    def prepare(self, home):
        source = self.fixture(home)
        with mock.patch.object(pilot, 'systemctl', side_effect=self.service), \
                mock.patch.object(pilot, 'docker', side_effect=self.docker):
            provision.provision(pilot, 'new-claude', source, BASE)
        return provision.load_profile('new-claude', states=('awaiting-login',))

    def test_prepare_creates_private_login_home_without_starting_or_copying_host_auth(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            self.setup_context(home)
            with mock.patch.object(pilot, 'systemctl', side_effect=self.service) as service, \
                    mock.patch.object(pilot, 'docker', side_effect=self.docker) as docker:
                provision.provision(pilot, 'new-claude', self.fixture(home), BASE)
            record = provision.load_profile('new-claude', states=('awaiting-login',))
            self.assertEqual(record['family'], 'claude')
            self.assertEqual(record['model'], MODEL)
            self.assertFalse(any(call.args[0] == 'enable' for call in service.call_args_list))
            args = claude_profile.runtime_args(pilot, DERIVED)
            mounts = [item for item in args if item.startswith('type=volume,')]
            self.assertEqual(mounts, [
                'type=volume,source=buzz-isolated-new-claude-workspace,target=/workspace',
                'type=volume,source=buzz-isolated-new-claude-home,target=/home/paul'])
            self.assertNotIn('type=bind', str(args))
            self.assertNotIn('ssh', str(args))
            self.assertNotIn('synthetic-claude-key', str(docker.call_args_list))
            with self.assertRaises(ValueError):
                pilot.start(DERIVED)

    def test_rejects_inherited_or_alternate_auth_and_model_aliases(self):
        with tempfile.TemporaryDirectory() as directory:
            source = self.fixture(Path(directory))
            original = source.read_text()
            for key in claude_profile.HOST_AUTH:
                source.write_text(original + key + '=must-not-be-forwarded\n')
                with self.subTest(key=key), self.assertRaises(ValueError):
                    provision.read_environment(source)
            source.write_text(original.replace(MODEL, 'fable'))
            with self.assertRaises(ValueError):
                provision.read_environment(source)
        self.assertFalse(claude_profile.HOST_AUTH.intersection(claude_profile.forwarded(pilot)))

    def test_missing_login_or_failed_model_check_never_enables_service(self):
        for failure in ['auth_status', 'model_check']:
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                home = Path(directory)
                stack = self.setup_context(home)
                self.prepare(home)
                with mock.patch.object(pilot, 'systemctl', side_effect=self.service) as service, \
                        mock.patch.object(pilot, 'docker', side_effect=self.docker), \
                        mock.patch.object(claude_profile, 'auth_status'), \
                        mock.patch.object(claude_profile, 'model_check'), \
                        mock.patch.object(claude_profile, failure, side_effect=ValueError('not verified')):
                    with self.assertRaises(ValueError):
                        claude_profile.activate(pilot, 'new-claude')
                self.assertFalse(any(call.args[0] == 'enable' for call in service.call_args_list))
                self.assertEqual(provision.load_profile('new-claude', states=('awaiting-login',))['state'], 'awaiting-login')
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
                    mock.patch.object(claude_profile, 'auth_status'), \
                    mock.patch.object(claude_profile, 'model_check'):
                with self.assertRaises(subprocess.CalledProcessError):
                    claude_profile.activate(pilot, 'new-claude')
            self.assertIn(('disable', '--now', 'buzz-isolated-new-claude.service'), calls)
            self.assertFalse(any('buzz-acp@' in str(args) for args in calls))
            self.assertEqual(provision.load_profile('new-claude', states=('awaiting-login',))['state'], 'awaiting-login')

    def test_api_key_status_is_not_accepted_as_private_subscription_login(self):
        record = {'image': DERIVED}
        for logged_in, method in [(False, 'none'), (True, 'api_key'), (True, 'oauth_token')]:
            status = subprocess.CompletedProcess([], 0, stdout=json.dumps({'loggedIn': logged_in, 'authMethod': method}))
            with mock.patch.object(claude_profile, 'helper', return_value=status):
                with self.assertRaises(ValueError):
                    claude_profile.auth_status(pilot, record)
        status = subprocess.CompletedProcess([], 0, stdout=json.dumps({'loggedIn': True, 'authMethod': 'claude.ai'}))
        with mock.patch.object(claude_profile, 'helper', return_value=status):
            claude_profile.auth_status(pilot, record)

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
                    mock.patch.object(claude_profile, 'auth_status', side_effect=lambda *_: steps.append('auth')), \
                    mock.patch.object(claude_profile, 'model_check', side_effect=lambda *_: steps.append('model')), \
                    mock.patch.object(claude_profile, 'audit', side_effect=lambda *_: steps.append('audit')):
                claude_profile.activate(pilot, 'new-claude')
            self.assertEqual(steps, ['auth', 'model', 'enable', 'audit', 'is-active'])
            self.assertEqual(provision.load_profile('new-claude')['model'], MODEL)

    def test_changed_pending_settings_cannot_activate_without_reverification(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            self.setup_context(home)
            self.prepare(home)
            source = home / 'buzz-acp-new-claude.env'
            source.write_text(source.read_text().replace(MODEL, 'claude-fable-5-1'))
            with mock.patch.object(pilot, 'systemctl', side_effect=self.service) as service, \
                    mock.patch.object(pilot, 'docker', side_effect=self.docker), \
                    mock.patch.object(claude_profile, 'auth_status') as auth:
                with self.assertRaisesRegex(ValueError, 'settings changed'):
                    claude_profile.activate(pilot, 'new-claude')
            auth.assert_not_called()
            self.assertFalse(any(call.args[0] == 'enable' for call in service.call_args_list))

    def test_login_requires_owner_terminal_and_helpers_preserve_existing_names(self):
        with mock.patch.object(claude_profile.os, 'isatty', return_value=False), \
                mock.patch.object(pilot, 'docker') as docker:
            with self.assertRaisesRegex(ValueError, 'interactive terminal'):
                claude_profile.login(pilot, 'new-claude')
            docker.assert_not_called()
        with mock.patch.object(pilot, 'docker', return_value=subprocess.CompletedProcess([], 0, stdout='existing')) as docker:
            with self.assertRaisesRegex(ValueError, 'Existing Claude helper'):
                claude_profile.helper(pilot, {'image': DERIVED}, 'reserved-helper', ['python3', '-c', 'pass'])
            self.assertEqual(len(docker.call_args_list), 1)

    def test_timed_out_helper_removes_its_exact_container(self):
        self.addCleanup(pilot.configure_profile, pilot.AGENT, pilot.UID, pilot.DISPLAY_NAME)
        pilot.configure_profile('new-claude', 62000, 'new-claude')
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
                claude_profile.helper(pilot, {'image': DERIVED}, 'new-claude-check', ['python3', '-c', 'pass'])
        self.assertEqual(removed, ['new-claude-check'])
        self.assertFalse(running)


@unittest.skipUnless(os.environ.get('CLAUDE_NEW_IMAGE'), 'Set CLAUDE_NEW_IMAGE for clean Claude storage/login journey')
class FreshClaudeJourney(unittest.TestCase):
    setup_context = test_provision.Provisioning.setup_context
    service = test_provision.Provisioning.service
    fixture = ClaudeProfile.fixture

    def test_real_private_home_uid_and_unauthenticated_cli(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            self.setup_context(home)
            name = 'claude-test-' + uuid.uuid4().hex[:12]
            prefix = 'buzz-isolated-' + name
            for suffix in ['-workspace', '-home']:
                self.addCleanup(subprocess.run, ['docker', 'volume', 'rm', prefix + suffix],
                                capture_output=True, timeout=30)
            with mock.patch.object(pilot, 'systemctl', side_effect=self.service):
                provision.provision(pilot, name, self.fixture(home, name), os.environ['CLAUDE_NEW_IMAGE'])
            record = provision.load_profile(name, states=('awaiting-login',))
            with self.assertRaisesRegex(ValueError, 'not signed in'):
                claude_profile.auth_status(pilot, record)
            code = ('import json,os,subprocess; from pathlib import Path; '
                    f'assert os.getuid()=={record["uid"]}; '
                    'h=Path("/home/paul"); assert not(h/".claude/.credentials.json").exists(), "unexpected credentials"; '
                    'assert not Path("/run/host").exists(); assert not Path("/var/run/docker.sock").exists(); '
                    'p=Path("/workspace/REPOS/probe"); p.mkdir(parents=True); '
                    'p.joinpath("result.txt").write_bytes(b"370\\n"); '
                    'subprocess.run(["git","init","-q",str(p)],check=True); '
                    'subprocess.run(["git","-C",str(p),"add","result.txt"],check=True); '
                    'h.joinpath("own-home.txt").write_text("private"); '
                    'policy=json.loads(Path("/etc/claude-code/managed-settings.json").read_text()); '
                    f'assert policy["availableModels"]==[{MODEL!r}]; '
                    'assert policy["switchModelsOnFlag"] is False; '
                    'print("fresh_claude_boundary_passed")')
            args = claude_profile.runtime_args(pilot, record['image'], name=prefix + '-probe',
                                              entrypoint='python3', network='none')
            try:
                result = pilot.docker([*args, record['image'], '-c', code], capture_output=True, text=True)
                self.assertIn('fresh_claude_boundary_passed', result.stdout)
            except subprocess.CalledProcessError as error:
                print((error.stderr or '')[-2000:])
                raise
            finally:
                claude_profile.remove_helper(pilot, prefix + '-probe')
            independent = ('from pathlib import Path; import subprocess; p=Path("/workspace/REPOS/probe"); '
                           f'assert p.joinpath("result.txt").stat().st_uid=={record["uid"]}; '
                           'assert p.joinpath("result.txt").read_bytes()==b"370\\n"; '
                           'assert subprocess.check_output(["git","-C",str(p),"show",":result.txt"])==b"370\\n"')
            claude_profile.helper(pilot, record, prefix + '-independent', ['python3', '-c', independent],
                                  readonly=True, capture_output=True, timeout=30)
            name = prefix + '-audit'
            args = claude_profile.runtime_args(pilot, record['image'], name=name,
                                              entrypoint='sleep', network='none')
            pilot.docker([*args, '--detach', record['image'], '60'], capture_output=True)
            try:
                claude_profile.audit(pilot, name, record['image'], network='none')
            finally:
                claude_profile.remove_helper(pilot, name)

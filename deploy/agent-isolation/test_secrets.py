"""Synthetic journeys through real age, terminal input, staging and launch seams."""
import contextlib
import io
import json
import os
from pathlib import Path
import pty
import select
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

import hive_secrets as secrets
import pilot
import native
import claude_profile
import provision

AGE = bool(shutil.which('age') and shutil.which('age-keygen'))
IMAGE = 'sha256:' + 'a' * 64


class SecretValidation(unittest.TestCase):
    def test_names_fields_and_agents_reject_paths_and_terminal_control(self):
        for name in ['', '../fizz', 'Globird', 'a/b', 'x' * 64, 'x\n']:
            with self.subTest(name=name), self.assertRaises(secrets.SecretError):
                secrets.validate_name(name)
        for field in ['', 'a\n', '\x1b[0m', 'x' * 65]:
            with self.subTest(field=field), self.assertRaises(secrets.SecretError):
                secrets.validate_field(field)
        self.assertEqual(secrets.validate_name('globird-2'), 'globird-2')
        self.assertEqual(secrets.validate_field('account number'), 'account number')
        with self.assertRaises((ValueError, OSError)):
            secrets.validate_agent('unknown-unregistered-agent')

    def test_absent_store_is_noop_and_nonterminal_input_refused(self):
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(Path, 'home', return_value=Path(directory)), \
                mock.patch.object(secrets, 'container_exists') as check:
            with secrets.delivery('fizz', 'buzz-isolated-fizz') as args:
                self.assertEqual(args, [])
            check.assert_not_called()
        with mock.patch.object(os, 'isatty', return_value=False):
            with self.assertRaises(secrets.SecretError):
                secrets.require_terminal()

    def test_unsafe_paths_and_interrupted_atomic_write(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / 'target'
            target.mkdir(mode=0o700)
            (root / 'link').symlink_to(target, target_is_directory=True)
            with self.assertRaises(secrets.SecretError):
                secrets.private_dir(root / 'link')
            target.chmod(0o755)
            with self.assertRaises(secrets.SecretError):
                secrets.private_dir(target)
            path = root / 'ciphertext'
            secrets.atomic_write(path, b'original')
            with mock.patch.object(secrets.os, 'replace', side_effect=OSError('synthetic failure')):
                with self.assertRaises(OSError):
                    secrets.atomic_write(path, b'replacement')
            self.assertEqual(path.read_bytes(), b'original')
            self.assertEqual(sorted(p.name for p in root.iterdir()), ['ciphertext', 'link', 'target'])
            path.chmod(0o644)
            with self.assertRaises(secrets.SecretError):
                secrets.read_private(path)
            path.unlink()
            path.symlink_to(target)
            with self.assertRaises(OSError):
                secrets.read_private(path)

    def test_mount_audit_allows_only_exact_readonly_agent_leaf(self):
        with mock.patch.object(secrets, 'runtime_root', return_value=Path('/run/user/1000/hive-secrets')):
            good = {'Type': 'bind', 'Destination': '/run/secrets', 'Source': '/run/user/1000/hive-secrets/fizz', 'RW': False}
            other = {'Type': 'volume', 'Destination': '/workspace'}
            self.assertEqual(secrets.audit_mounts([other, good], 'fizz'), [other])
            for change in [{'RW': True}, {'Type': 'volume'}, {'Source': '/tmp/fizz'},
                           {'Source': '/run/user/1000/hive-secrets/north'}]:
                with self.subTest(change=change), self.assertRaises(secrets.SecretError):
                    secrets.audit_mounts([{**good, **change}], 'fizz')
            with self.assertRaises(secrets.SecretError):
                secrets.audit_mounts([good, good], 'fizz')


@unittest.skipUnless(AGE, 'Install age and age-keygen to exercise real encryption')
class SecretJourney(unittest.TestCase):
    def setUp(self):
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.runtime = Path(self.enterContext(tempfile.TemporaryDirectory(dir='/dev/shm')))
        self.enterContext(mock.patch.object(Path, 'home', return_value=self.home))
        self.enterContext(mock.patch.dict(os.environ, {'XDG_RUNTIME_DIR': str(self.runtime)}))
        self.store = secrets.Store()
        self.store.init()
        self.sentinel = 'SYNTHETIC-DO-NOT-LOG-938475'
        self.addCleanup(pilot.configure, pilot.AGENT)

    def put(self, agents=None):
        with self.store.locked():
            self.store.set('test-secret', {'username': 'synthetic', 'password': self.sentinel},
                           ['fizz'] if agents is None else agents)

    def test_store_cli_metadata_grant_revoke_remove_and_private_modes(self):
        self.put()
        self.assertNotIn(self.sentinel.encode(), (self.store.encrypted / 'test-secret.age').read_bytes())
        for path in [self.store.identity, self.store.grants_path, self.store.encrypted / 'test-secret.age']:
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        for path in [self.store.root, self.store.encrypted, self.store.state]:
            self.assertEqual(path.stat().st_mode & 0o777, 0o700)
        identity = self.store.identity.read_bytes()
        with self.assertRaises(secrets.SecretError):
            self.store.init()
        self.assertEqual(self.store.identity.read_bytes(), identity)
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            secrets.main(['list'])
            secrets.main(['grant', 'test-secret', 'north'])
            secrets.main(['revoke', 'test-secret', 'fizz'])
        self.assertNotIn(self.sentinel, stdout.getvalue())
        self.assertIn('password', stdout.getvalue())
        self.assertIn('Rotate', stdout.getvalue())
        with self.store.locked():
            self.assertEqual(self.store.grants()['test-secret']['agents'], ['north'])
            self.store.remove('test-secret')
            self.assertEqual(self.store.metadata(), [])
        self.assertFalse((self.store.encrypted / 'test-secret.age').exists())

    def test_delivery_revocation_concurrency_failed_launch_and_stale_recovery(self):
        self.put()
        leaf = self.runtime / 'hive-secrets/fizz'
        with mock.patch.object(secrets, 'container_exists', return_value=False):
            with secrets.delivery('fizz', 'buzz-isolated-fizz') as args:
                self.assertNotIn(self.sentinel, str(args))
                self.assertIn('target=/run/secrets,readonly', args[1])
                self.assertEqual(leaf.stat().st_mode & 0o777, 0o755)
                path = leaf / 'test-secret.json'
                self.assertEqual(path.stat().st_mode & 0o777, 0o444)
                self.assertEqual(json.loads(path.read_bytes())['fields']['password'], self.sentinel)
                self.assertEqual(leaf.parent.stat().st_mode & 0o777, 0o700)
                with self.store.locked():
                    self.store.grant('test-secret', 'fizz', revoke=True)
                self.assertTrue(path.exists())
                with self.store.locked():
                    self.store.grant('test-secret', 'fizz')
                with self.assertRaises(secrets.SecretError):
                    with secrets.delivery('fizz', 'buzz-isolated-fizz'):
                        self.fail('Concurrent launcher acquired the leaf')
                self.assertTrue(path.exists())
            self.assertFalse(leaf.exists())
            with self.assertRaises(RuntimeError):
                with secrets.delivery('fizz', 'buzz-isolated-fizz'):
                    raise RuntimeError('synthetic docker run failure')
            self.assertFalse(leaf.exists())
            leaf.mkdir(mode=0o755)
            (leaf / 'old.json').write_text('synthetic stale value')
            with mock.patch.object(secrets, 'container_exists', return_value=True):
                with self.assertRaises(secrets.SecretError):
                    with secrets.delivery('fizz', 'buzz-isolated-fizz'):
                        self.fail('Reused existing container')
            self.assertTrue((leaf / 'old.json').exists())
            with self.store.locked():
                self.store.grant('test-secret', 'fizz', revoke=True)
            with secrets.delivery('fizz', 'buzz-isolated-fizz') as args:
                self.assertEqual(args, [])
                self.assertFalse(leaf.exists())
            with secrets.delivery('north', 'buzz-isolated-north') as args:
                self.assertEqual(args, [])

    def test_corruption_and_persistent_runtime_fail_closed_without_value_in_error(self):
        self.put()
        with mock.patch.object(secrets, 'container_exists', return_value=False):
            (self.store.encrypted / 'test-secret.age').write_bytes(b'invalid ' + self.sentinel.encode())
            with self.assertRaises(secrets.SecretError) as caught:
                with secrets.delivery('fizz', 'buzz-isolated-fizz'):
                    self.fail('Corrupt secret reached launch')
            self.assertNotIn(self.sentinel, str(caught.exception))
            self.assertIn('test-secret', str(caught.exception))
            self.assertFalse((self.runtime / 'hive-secrets/fizz').exists())
            with mock.patch.object(secrets.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, stdout='ext4\n')):
                with self.assertRaises(secrets.SecretError):
                    secrets.runtime_root()

    def test_interrupted_set_revokes_access_before_changing_ciphertext(self):
        self.put()
        original = self.store.save_grants
        writes = 0

        def fail_final(value):
            nonlocal writes
            writes += 1
            if writes == 2:
                raise OSError('synthetic disk error')
            original(value)

        with self.store.locked(), mock.patch.object(self.store, 'save_grants', side_effect=fail_final):
            with self.assertRaises(OSError):
                self.store.set('test-secret', {'password': 'replacement'}, ['north'])
            self.assertNotIn('test-secret', self.store.grants())
        with mock.patch.object(secrets, 'container_exists', return_value=False):
            for agent in ['fizz', 'north']:
                with secrets.delivery(agent, 'buzz-isolated-' + agent) as args:
                    self.assertEqual(args, [])

    def test_all_three_production_launch_paths_use_delivery_and_cleanup(self):
        for family in ['pilot', 'native', 'fresh-claude']:
            with self.subTest(family=family):
                agent = 'north' if family == 'pilot' else 'codex' if family == 'native' else 'claude-haiku'
                pilot.configure_profile(agent, 61034, agent)
                self.put([agent])
                env = {'BUZZ_PRIVATE_KEY': 'synthetic', 'BUZZ_RELAY_URL': 'wss://relay.invalid',
                       'BUZZ_AGENT_PROVIDER': 'openrouter', 'OPENROUTER_API_KEY': 'synthetic',
                       'OPENROUTER_MODEL': 'synthetic', 'BUZZ_ACP_MODEL': 'claude-fable-5-2'}

                def docker(args, **kwargs):
                    value = 'codex' if family == 'native' and args[0] == 'image' else '127.0.0.1'
                    return subprocess.CompletedProcess(args, 0, stdout=value)

                def launch(args):
                    self.assertNotIn(self.sentinel, str(args))
                    self.assertIn('target=/run/secrets,readonly', str(args))
                    path = self.runtime / 'hive-secrets' / agent / 'test-secret.json'
                    self.assertEqual(json.loads(path.read_bytes())['fields']['password'], self.sentinel)
                    raise OSError('synthetic launch failure')

                with mock.patch.dict(os.environ, env), \
                        mock.patch.object(secrets, 'container_exists', return_value=False), \
                        mock.patch.object(pilot, 'docker', side_effect=docker), \
                        mock.patch.object(pilot.socket, 'getaddrinfo', return_value=[(2, 1, 6, '', ('127.0.0.1', 443))]), \
                        mock.patch.object(provision, 'load_profile', return_value={
                            'family': 'claude', 'image': IMAGE, 'model': env['BUZZ_ACP_MODEL']}), \
                        mock.patch.object(claude_profile, 'public_routes', return_value=[]), \
                        mock.patch.object(pilot.subprocess, 'call', side_effect=launch):
                    with self.assertRaises(OSError):
                        if family == 'pilot':
                            pilot.start(IMAGE)
                        elif family == 'native':
                            native.start(IMAGE)
                        else:
                            claude_profile.start(pilot, IMAGE)
                self.assertFalse((self.runtime / 'hive-secrets' / agent).exists())

    def test_real_terminal_entry_hides_both_username_and_password(self):
        master, slave = pty.openpty()
        command = ['python3', str(Path(__file__).with_name('hive-secret')),
                   'set', 'terminal-secret', '--agent', 'fizz']
        proc = subprocess.Popen(command, stdin=slave, stdout=slave, stderr=slave,
                                start_new_session=True, env={**os.environ, 'HOME': str(self.home)})
        os.close(slave)
        captured = b''
        try:
            for prompt, value in [(b'username: ', b'SYNTHETIC-USER'), (b'password: ', self.sentinel.encode())]:
                for _ in range(100):
                    if select.select([master], [], [], 0.1)[0]:
                        captured += os.read(master, 4096)
                    if prompt in captured:
                        break
                self.assertIn(prompt, captured)
                os.write(master, value + b'\n')
            self.assertEqual(proc.wait(timeout=15), 0)
            while select.select([master], [], [], 0)[0]:
                try:
                    captured += os.read(master, 4096)
                except OSError:
                    break
            self.assertNotIn(self.sentinel.encode(), captured)
            self.assertNotIn(b'SYNTHETIC-USER', captured)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            os.close(master)
        with self.store.locked():
            self.assertEqual(json.loads(self.store.decrypt('terminal-secret'))['fields']['password'], self.sentinel)


@unittest.skipUnless(AGE and os.environ.get('HIVE_SECRETS_TEST_IMAGE'),
                     'Set HIVE_SECRETS_TEST_IMAGE with age for disposable Docker delivery')
class DockerSecretJourney(unittest.TestCase):
    setUp = SecretJourney.setUp
    put = SecretJourney.put
    def test_real_cross_uid_mount_inspect_other_container_and_revoke(self):
        import hashlib
        import uuid
        image = pilot.image_id(os.environ['HIVE_SECRETS_TEST_IMAGE'])
        self.put()
        name = 'hive-secret-test-' + uuid.uuid4().hex[:12]

        def docker(args, **kwargs):
            return subprocess.run(['docker', *args], check=True, capture_output=True,
                                  text=True, timeout=20, **kwargs)

        def args(container):
            return ['run', '--rm', '--name', container, '--network', 'none',
                    '--user', '61022:61022', '--cap-drop', 'ALL', '--read-only',
                    '--security-opt', 'no-new-privileges:true', '--log-driver', 'none',
                    '--tmpfs', '/run:rw,noexec,nosuid,nodev,size=16m,mode=755',
                    '--entrypoint', 'python3']

        try:
            with secrets.delivery('fizz', name) as mount:
                docker([*args(name), '--detach', *mount, image, '-c', 'import time;time.sleep(60)'])
                inspected = docker(['inspect', name]).stdout
                self.assertNotIn(self.sentinel, inspected)
                data = json.loads(inspected)[0]
                binds = [m for m in data['Mounts'] if m['Type'] == 'bind']
                self.assertEqual(binds[0]['Destination'], '/run/secrets')
                self.assertFalse(binds[0]['RW'])
                code = ('import json,hashlib,os; from pathlib import Path; '
                        'assert os.getuid()==61022; p=Path("/run/secrets/test-secret.json"); '
                        'v=json.loads(p.read_bytes())["fields"]["password"]; '
                        'print(hashlib.sha256(v.encode()).hexdigest()); '
                        'assert not os.access(p,os.W_OK)')
                read = docker(['exec', '--interactive', name, 'python3', '-'], input=code).stdout.strip()
                self.assertEqual(read, hashlib.sha256(self.sentinel.encode()).hexdigest())
                with secrets.delivery('north', name + '-other') as other_mount:
                    self.assertEqual(other_mount, [])
                    docker([*args(name + '-other'), image, '-c',
                            'from pathlib import Path; assert not Path("/run/secrets/test-secret.json").exists()'])
                docker(['rm', '--force', name])
            self.assertFalse((self.runtime / 'hive-secrets/fizz').exists())
            with self.store.locked():
                self.store.grant('test-secret', 'fizz', revoke=True)
            with secrets.delivery('fizz', name) as revoked:
                self.assertEqual(revoked, [])
                docker([*args(name), image, '-c',
                        'from pathlib import Path; assert not Path("/run/secrets/test-secret.json").exists()'])
        finally:
            subprocess.run(['docker', 'rm', '--force', name, name + '-other'],
                           capture_output=True, timeout=20)

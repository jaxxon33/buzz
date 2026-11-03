"""Production workspace-copy and provider-preservation journeys."""
import contextlib
import importlib.util
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock
import uuid

import workspace

SPEC = importlib.util.spec_from_file_location('batch_pilot', Path(__file__).with_name('pilot.py'))
pilot = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(pilot)


class WorkspacePolicy(unittest.TestCase):
    def test_rejects_links_outside_workspace_before_copy(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root/'external').symlink_to('/home/paul/.ssh')
            with self.assertRaises(ValueError):
                workspace.validate(root)

    def test_openrouter_and_sender_allowlist_are_preserved_without_openai_credentials(self):
        pilot.configure('bumble')
        env = {'BUZZ_PRIVATE_KEY': 'a'*64, 'BUZZ_RELAY_URL': 'wss://relay.invalid',
               'BUZZ_AGENT_PROVIDER': 'openrouter', 'OPENROUTER_API_KEY': 'synthetic-provider-secret',
               'OPENROUTER_MODEL': 'unchanged-model', 'OPENROUTER_BASE_URL': 'https://provider.invalid/v1',
               'BUZZ_ACP_RESPOND_TO_ALLOWLIST': 'unchanged-allowlist'}
        tunnel = mock.Mock();tunnel.start.return_value=12345

        def docker(args, **kwargs):
            return subprocess.CompletedProcess([], 0, stdout='172.17.0.1' if args[:2]==['network','inspect'] else '')

        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(pilot,'docker',side_effect=docker), \
             mock.patch.object(pilot.socket,'getaddrinfo',return_value=[(2,1,6,'',('127.0.0.1',443))]), \
             mock.patch.object(pilot,'Tunnel',return_value=contextlib.nullcontext(tunnel)), \
             mock.patch.object(pilot.subprocess,'call',return_value=0) as launch:
            self.assertEqual(pilot.start('sha256:'+'a'*64),0)
            args=launch.call_args.args[0]
            for key in ['OPENROUTER_API_KEY','OPENROUTER_MODEL','OPENROUTER_BASE_URL','BUZZ_ACP_RESPOND_TO_ALLOWLIST']:
                self.assertIn(key,args)
            self.assertNotIn('synthetic-provider-secret',' '.join(args))
            self.assertNotIn('target=/run/fizz-ssh',' '.join(args))


@unittest.skipUnless(os.environ.get('PILOT_IMAGE'),'Set PILOT_IMAGE for real workspace journey')
class WorkspaceJourney(unittest.TestCase):
    def test_content_links_and_worker_writes_survive_copy_and_existing_volume_is_not_overwritten(self):
        pilot.configure(os.environ.get('PILOT_AGENT','gemma'))
        image=os.environ['PILOT_IMAGE']
        volume='buzz-workspace-test-'+uuid.uuid4().hex[:12]
        subprocess.run(['docker','volume','create',volume],check=True,capture_output=True,timeout=15)
        try:
            with tempfile.TemporaryDirectory() as d:
                source=Path(d);(source/'saved').mkdir();(source/'saved'/'data.txt').write_text('saved state\n')
                (source/'link.txt').symlink_to('saved/data.txt')
                with mock.patch.object(pilot,'VOLUME',volume):
                    pilot.migrate_workspace(image,source)
                    code="""import os,pathlib
assert os.getuid()==UID
assert pathlib.Path('/workspace/link.txt').read_text()=='saved state\\n'
assert pathlib.Path('/workspace/link.txt').is_symlink()
pathlib.Path('/workspace/new.txt').write_text('new work')
""".replace('UID',str(pilot.UID))
                    args=pilot.runtime_args(image,name='check-'+volume,volume=volume,entrypoint='python3',network='none')
                    subprocess.run(['docker',*args,image,'-c',code],check=True,capture_output=True,text=True,timeout=20)
                    with self.assertRaises(subprocess.CalledProcessError):
                        pilot.migrate_workspace(image,source)
        finally:
            subprocess.run(['docker','volume','rm',volume],check=True,capture_output=True,timeout=20)

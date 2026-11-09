"""Credential-free native sandbox check, run only in disposable test volumes."""
import json
import os
from pathlib import Path
import socket
import subprocess


def probe():
    Path(os.environ.get('CODEX_HOME', '/home/paul/.codex')).mkdir(parents=True, exist_ok=True)
    control = Path('/home/paul/native-outside-control.txt')
    control.write_text('parent can write')
    control.write_text('retained')
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        listener.listen(4)
        address = listener.getsockname()
        with socket.create_connection(address, timeout=1):
            pass
        worker = '''
import errno,json,os,socket,subprocess
from pathlib import Path
assert os.getuid()==UID
status=Path('/proc/self/status').read_text()
assert 'NoNewPrivs:\\t1' in status and 'CapEff:\\t0000000000000000' in status
repo=Path('/workspace/REPOS/native-sandbox-boundary');repo.mkdir(parents=True)
(repo/'result.txt').write_bytes(b'370\\n')
subprocess.run(['git','init','-q',str(repo)],check=True)
subprocess.run(['git','-C',str(repo),'add','result.txt'],check=True)
checks={}
try:
 Path('/home/paul/native-outside-control.txt').write_text('overwritten')
 checks['outside_write_denied']=False
except OSError as error:
 checks['outside_write_denied']=error.errno in [errno.EACCES,errno.EPERM,errno.EROFS]
 checks['outside_write_errno']=error.errno
try:
 with socket.create_connection(ADDRESS,timeout=1):pass
 checks['network_denied']=False
except OSError as error:
 checks['network_denied']=error.errno in [errno.EPERM,errno.EACCES,errno.ENETUNREACH,errno.EHOSTUNREACH,errno.ECONNREFUSED]
 checks['network_errno']=error.errno
print(json.dumps(checks))
assert checks['outside_write_denied'] and checks['network_denied']
'''.replace('UID', str(os.getuid())).replace('ADDRESS', repr(address))
        command = ['/opt/buzz/native/codex/node_modules/.bin/codex', 'sandbox',
                   '-c', 'sandbox_mode="workspace-write"', '-c', 'approval_policy="on-request"',
                   '-c', 'approvals_reviewer="auto_review"', '--', 'python3', '-c', worker]
        result = subprocess.run(command, cwd='/workspace', capture_output=True, text=True, timeout=30)
        if result.returncode:
            raise RuntimeError('Native sandbox boundary probe failed: ' + result.stderr[:2048])
        checks = json.loads(result.stdout)
    artifact = Path('/workspace/REPOS/native-sandbox-boundary/result.txt')
    assert control.read_text() == 'retained'
    assert artifact.read_bytes() == b'370\n' and artifact.stat().st_uid == os.getuid()
    assert subprocess.check_output(['git', '-C', str(artifact.parent), 'show', ':result.txt']) == b'370\n'
    return {'native_inner_sandbox': 'passed', 'uid': os.getuid(), **checks}


if __name__ == '__main__':
    print(json.dumps(probe()))

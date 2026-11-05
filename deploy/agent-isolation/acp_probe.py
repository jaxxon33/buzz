#!/usr/bin/env python3
"""Bounded real native model/tool journey, run INSIDE the worker boundary."""
import argparse
import json
import os
from pathlib import Path
import selectors
import re
import subprocess
import time


def probe(command, cwd, model, seconds, label='native-check-20261104', with_mcp=False):
    if not re.fullmatch(r'[a-z0-9-]+', label):
        raise ValueError('Unsupported probe label')
    child = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, text=True, bufsize=1, cwd=cwd)
    selector = selectors.DefaultSelector()
    selector.register(child.stdout, selectors.EVENT_READ)
    counter = 0
    pending = b''
    updates = {}

    def send(value):
        child.stdin.write(json.dumps(value) + '\n')
        child.stdin.flush()

    def request(method, params):
        nonlocal counter, pending
        counter += 1
        number = counter
        send({'jsonrpc': '2.0', 'id': number, 'method': method, 'params': params})
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            # TextIOWrapper.readline() may prefetch the next RPC reply.
            # Read raw bytes and drain complete buffered lines before polling
            # the descriptor again, including updates batched with completion.
            if b'\n' not in pending:
                if not selector.select(max(0, deadline - time.monotonic())):
                    break
                chunk = os.read(child.stdout.fileno(), 65536)
                if not chunk:
                    raise RuntimeError('Adapter exited')
                pending += chunk
                if b'\n' not in pending:
                    continue
            line, pending = pending.split(b'\n', 1)
            message = json.loads(line)
            if message.get('method') == 'session/update':
                update = message.get('params', {}).get('update', {})
                kind = update.get('sessionUpdate', 'unknown')
                updates[kind] = updates.get(kind, 0) + 1
            if message.get('id') == number and 'method' not in message:
                if 'error' in message:
                    # Provider errors are not emitted: they can echo request state.
                    raise RuntimeError(f'ACP {method} returned error {message["error"].get("code")}')
                return message.get('result', {})
            if message.get('method') == 'session/request_permission':
                choice = next((x for x in message['params']['options'] if x['kind'] == 'allow_once'), None)
                outcome = {'outcome': 'selected', 'optionId': choice['optionId']} if choice else {'outcome': 'cancelled'}
                send({'jsonrpc': '2.0', 'id': message['id'], 'result': {'outcome': outcome}})
            elif 'method' in message and 'id' in message:
                send({'jsonrpc': '2.0', 'id': message['id'],
                      'error': {'code': -32601, 'message': 'Probe does not offer client-side tools'}})
        raise TimeoutError('Native ACP request deadline expired')

    try:
        initialized = request('initialize', {'protocolVersion': 1,
            'clientInfo': {'name': 'buzz-isolation-probe', 'version': '1'},
            'clientCapabilities': {'fs': {'readTextFile': False, 'writeTextFile': False}, 'terminal': False}})
        servers = [{'name': 'dev', 'command': '/opt/buzz/bin/buzz-dev-mcp', 'args': [], 'env': []}] if with_mcp else []
        session = request('session/new', {'cwd': cwd, 'mcpServers': servers})
        session_id = session['sessionId']
        if model:
            option = next((x for x in session.get('configOptions', [])
                           if x.get('category') == 'model' and
                           any(v.get('value') == model for v in x.get('options', []))), None)
            if option:
                request('session/set_config_option', {'sessionId': session_id,
                        'configId': option.get('configId', option.get('id')), 'value': model})
            elif any(x.get('modelId') == model for x in session.get('models', {}).get('availableModels', [])):
                request('session/set_model', {'sessionId': session_id, 'modelId': model})
            else:
                raise ValueError('Configured model is not advertised by this adapter')
        expected = Path(cwd) / 'REPOS' / label / 'result.txt'
        if expected.exists():
            raise ValueError('Probe artifact already exists')
        script = '''from pathlib import Path; import os,subprocess; p=Path("REPOS/LABEL"); p.mkdir(parents=True,exist_ok=True); (p/"result.txt").write_text("370\\n"); subprocess.run(["git","init","-q",str(p)],check=True); subprocess.run(["git","-C",str(p),"add","result.txt"],check=True); print(os.getuid(),(p/"result.txt").read_text().strip())'''.replace('LABEL', label)
        prompt = ('Run only this actual shell command in the current directory: python3 -c ' +
                  "'" + script + "'" + '. Then reply NATIVE_CHECK_DONE. Do not read credentials, settings or other files. Do not install, commit or push.')
        result = request('session/prompt', {'sessionId': session_id,
                         'prompt': [{'type': 'text', 'text': prompt}]})
        print(json.dumps({'native_session_updates': updates, 'stop_reason': result.get('stopReason')}), flush=True)
        if expected.read_text() != '370\n':
            raise RuntimeError('Actual artifact does not match')
        staged = subprocess.check_output(['git', '-C', str(expected.parent), 'diff', '--cached', '--name-only'], text=True).strip()
        if staged != 'result.txt':
            raise RuntimeError('Actual Git index does not match')
        print(json.dumps({'native_model_tools': 'passed', 'uid': os.getuid(),
                          'artifact': '370', 'git_staged': staged,
                          'stop_reason': result.get('stopReason'),
                          'agent': initialized.get('agentInfo', {}).get('name')}))
    finally:
        child.terminate()
        try:
            child.wait(timeout=10)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait()
        selector.close()
        child.stdin.close()
        child.stdout.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cwd', required=True)
    parser.add_argument('--model')
    parser.add_argument('--seconds', type=int, default=90)
    parser.add_argument('--label', default='native-check-20261104')
    parser.add_argument('--with-mcp', action='store_true')
    parser.add_argument('command', nargs='+')
    args = parser.parse_args()
    probe(args.command, args.cwd, args.model, args.seconds, args.label, args.with_mcp)

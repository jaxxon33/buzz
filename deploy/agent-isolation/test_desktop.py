"""Exercise merge routing and secondary failure; real client in opt-in image."""
import json
import os
from pathlib import Path
import selectors
import subprocess
import sys
import tempfile
import unittest

HERE = Path(__file__).parent


class MergeJourney(unittest.TestCase):
    def run_merge(self, secondary_crashes=False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            child = root / "child.py"
            child.write_text('''import json,sys,time
name=sys.argv[1]
if name=='dead':
 time.sleep(.1)
 sys.exit(1)
for line in sys.stdin:
 m=json.loads(line)
 if 'id' not in m:continue
 method=m.get('method')
 r={'tools':[{'name':name}]} if method=='tools/list' else {'server':name}
 print(json.dumps({'jsonrpc':'2.0','id':m['id'],'result':r}),flush=True)
''')
            config = root / "merge.json"
            config.write_text(json.dumps({"servers": [
                {"name": "dev", "command": sys.executable, "args": [str(child), "shell"]},
                {"name": "desktop", "command": sys.executable,
                 "args": [str(child), "dead" if secondary_crashes else "status"]},
            ]}))
            p = subprocess.Popen([sys.executable, str(HERE / "mcp_merge.py"), str(config)],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True)
            try:
                if secondary_crashes:
                    p.wait(timeout=5)
                    self.assertNotEqual(p.returncode, 0)
                    return
                requests = [
                    {"id": 1, "method": "initialize", "params": {}},
                    {"id": 2, "method": "tools/list", "params": {}},
                    {"id": 3, "method": "tools/call", "params": {"name": "shell"}},
                    {"id": 4, "method": "tools/call", "params": {"name": "status"}},
                ]
                with selectors.DefaultSelector() as selector:
                    selector.register(p.stdout, selectors.EVENT_READ)
                    results = []
                    for request in requests:
                        p.stdin.write(json.dumps(request) + '\n');p.stdin.flush()
                        self.assertTrue(selector.select(5), 'merge response timed out')
                        results.append(json.loads(p.stdout.readline()))
                self.assertEqual([t['name'] for t in results[1]['result']['tools']], ['shell', 'status'])
                self.assertEqual(results[2]['result']['server'], 'shell')
                self.assertEqual(results[3]['result']['server'], 'status')
            finally:
                if p.poll() is None:
                    p.terminate()
                p.communicate(timeout=8)

    def test_routes_dev_and_desktop_tools(self):
        self.run_merge()

    def test_dead_secondary_terminates_merge_instead_of_hanging_requests(self):
        self.run_merge(secondary_crashes=True)


@unittest.skipUnless(os.environ.get('PILOT_DESKTOP_IMAGE'), 'Set PILOT_DESKTOP_IMAGE for real merge check')
class DesktopImageJourney(unittest.TestCase):
    def test_real_merged_tools_and_shell_work_without_desktop_connection(self):
        image = os.environ['PILOT_DESKTOP_IMAGE']
        code = r'''
import json,os,selectors,subprocess
assert os.getuid()==61022
p=subprocess.Popen(['/opt/buzz/bin/fizz-mcp'],stdin=subprocess.PIPE,stdout=subprocess.PIPE,text=True)
s=selectors.DefaultSelector();s.register(p.stdout,selectors.EVENT_READ)
def call(i,method,params):
 p.stdin.write(json.dumps({'jsonrpc':'2.0','id':i,'method':method,'params':params})+'\n');p.stdin.flush()
 assert s.select(15),'MCP response timeout'
 r=json.loads(p.stdout.readline());assert r['id']==i and 'error' not in r,r
 return r['result']
try:
 call(1,'initialize',{'protocolVersion':'2024-11-05','capabilities':{},'clientInfo':{'name':'test','version':'1'}})
 tools=call(2,'tools/list',{})['tools'];names={t['name'] for t in tools}
 assert {'shell','status','lease_acquire','screenshot','click_element'}<=names,names
 result=call(3,'tools/call',{'name':'shell','arguments':{'command':'echo MERGED_SHELL_PASS'}})
 assert not result.get('isError') and 'MERGED_SHELL_PASS' in json.dumps(result),result
 print('MERGED_REAL_TOOLS_PASS')
finally:
 p.terminate();p.wait(timeout=8)
'''
        result = subprocess.run(['docker', 'run', '--rm', '--init', '--user', '61022:61022',
                                 '--network', 'none', '--read-only', '--cap-drop', 'ALL',
                                 '--security-opt', 'no-new-privileges', '--tmpfs', '/tmp:rw,noexec,nosuid,nodev',
                                 '--entrypoint', 'python3', image, '-c', code],
                                capture_output=True, text=True, timeout=45)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('MERGED_REAL_TOOLS_PASS', result.stdout)

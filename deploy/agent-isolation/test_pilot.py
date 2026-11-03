"""Run all tests with PILOT_IMAGE=sha256:... python3 -m unittest discover -v."""
import importlib.util
import json
import os
import shutil
import socket
import threading
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock
import uuid

SPEC = importlib.util.spec_from_file_location("pilot", Path(__file__).with_name("pilot.py"))
pilot = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(pilot)
pilot.configure(os.environ.get("PILOT_AGENT", "gemma"))


class Configuration(unittest.TestCase):
    def test_agent_profiles_have_distinct_authority_and_workspaces(self):
        original = pilot.AGENT
        profiles = []
        try:
            for agent, expected_uid in (("gemma", 61021), ("fizz", 61022)):
                pilot.configure(agent)
                args = pilot.runtime_args("sha256:" + "a" * 64)
                self.assertEqual(args[args.index("--user") + 1], f"{expected_uid}:{expected_uid}")
                self.assertIn(f"type=volume,source={pilot.VOLUME},target=/workspace", args)
                profiles.append((pilot.NAME, pilot.VOLUME, pilot.UID))
            for column in range(3):
                self.assertEqual(len({p[column] for p in profiles}), 2)
            with self.assertRaises(ValueError):
                pilot.configure("../../sol")
        finally:
            pilot.configure(original)

    def test_refuses_mutable_images_and_never_forwards_host_control(self):
        for value in ("image:latest", "sha256:abc", "--privileged", ""):
            with self.subTest(value=value), self.assertRaises(ValueError):
                pilot.runtime_args(value)
        self.assertFalse(pilot.FORWARDED.intersection({
            "HOME", "PATH", "SSH_AUTH_SOCK", "DOCKER_HOST", "LD_PRELOAD",
            "BUZZ_ACP_LAUNCH_PREFIX", "BUZZ_ACP_AGENT_COMMAND",
            "BUZZ_ACP_MCP_COMMAND", "BUZZ_ACP_BASE_PROMPT_FILE", "HTTP_PROXY",
        }))


class RelayTransportJourney(unittest.TestCase):
    def test_opaque_relay_bytes_round_trip_without_destination_selection(self):
        with socket.socket() as upstream:
            upstream.bind(("127.0.0.1", 0))
            upstream.listen(1)
            upstream.settimeout(5)

            def echo():
                connection, _ = upstream.accept()
                with connection:
                    connection.settimeout(5)
                    while data := connection.recv(65536):
                        connection.sendall(data)

            worker = threading.Thread(target=echo, daemon=True)
            worker.start()
            with pilot.Tunnel(("127.0.0.1", 0), upstream.getsockname()) as tunnel:
                port = tunnel.start()
                payload = bytes(range(256)) * 1024
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=5) as client:
                        client.sendall(payload)
                        received = b""
                        while len(received) < len(payload):
                            part = client.recv(65536)
                            self.assertTrue(part)
                            received += part
                        self.assertEqual(received, payload)
                finally:
                    tunnel.shutdown()
            worker.join(timeout=5)
            self.assertFalse(worker.is_alive())


class MigrationJourney(unittest.TestCase):
    def test_failed_start_restores_original_only_after_pilot_stops(self):
        for old_pid in ("0", "123"):
            with self.subTest(old_pid=old_pid), tempfile.TemporaryDirectory() as directory:
                home = Path(directory)
                (home / ".config/systemd/user").mkdir(parents=True)
                env_file = home / "agent.env"
                env_file.touch()
                calls = []
                state = {"pilot": False}

                def service(*args):
                    calls.append(args)
                    if args == ("daemon-reload",):
                        unit = home / ".config/systemd/user" / (pilot.NAME + ".service")
                        if unit.exists() and shutil.which("systemd-analyze"):
                            parsed = subprocess.run(["systemd-analyze", "--user", "verify", str(unit)],
                                                    capture_output=True, text=True, timeout=20)
                            self.assertNotIn("path is not absolute", parsed.stderr)
                    if args[:2] == ("show", pilot.ORIGINAL_SERVICE):
                        value = "active" if "--property=ActiveState" in args else old_pid
                        return subprocess.CompletedProcess(args, 0, stdout=value + "\n")
                    if args == ("enable", "--now", pilot.NAME + ".service"):
                        state["pilot"] = True
                        raise subprocess.CalledProcessError(1, ["systemctl"])
                    if args == ("disable", "--now", pilot.NAME + ".service"):
                        state["pilot"] = False
                    if args == ("enable", "--now", pilot.ORIGINAL_SERVICE):
                        self.assertFalse(state["pilot"], "duplicate live identities")
                    return subprocess.CompletedProcess(args, 0, stdout="")

                with mock.patch.object(pilot.Path, "home", return_value=home), \
                     mock.patch.object(pilot, "systemctl", side_effect=service), \
                     mock.patch.object(pilot, "docker", return_value=subprocess.CompletedProcess([], 0, stdout="")):
                    with self.assertRaises((subprocess.CalledProcessError, RuntimeError)):
                        pilot.activate("sha256:" + "a" * 64, env_file)
                self.assertIn(("enable", "--now", pilot.ORIGINAL_SERVICE), calls)
                self.assertFalse((home / ".config/systemd/user" / (pilot.NAME + ".service")).exists())
                if old_pid != "0":
                    self.assertNotIn(("enable", "--now", pilot.NAME + ".service"), calls)


@unittest.skipUnless(os.environ.get("PILOT_IMAGE"), "Set PILOT_IMAGE for real Docker/MCP checks")
class BoundaryJourney(unittest.TestCase):
    def test_real_tools_and_repo_work_without_host_access(self):
        image = pilot.image_id(os.environ["PILOT_IMAGE"])
        suffix = uuid.uuid4().hex[:12]
        volume = "buzz-pilot-test-" + suffix
        name = volume
        # A world-readable host marker proves filesystem isolation, not just chmod.
        with tempfile.NamedTemporaryFile(prefix="buzz-sibling-canary-", mode="w") as marker:
            marker.write("synthetic sibling secret\n")
            marker.flush()
            os.chmod(marker.name, 0o644)
            subprocess.run(["docker", "volume", "create", "--label", pilot.LABEL, volume],
                           check=True, capture_output=True, timeout=20)
            code = r'''
import json, os, pathlib, selectors, subprocess, sys, time
canary = sys.argv[1]
assert os.getuid() == 61021 and os.getgid() == 61021
assert os.getgroups() == [61021], os.getgroups()
status = pathlib.Path('/proc/self/status').read_text()
assert 'NoNewPrivs:\t1' in status
assert 'CapEff:\t0000000000000000' in status
root_mount = next(line for line in pathlib.Path('/proc/mounts').read_text().splitlines() if line.split()[1] == '/')
assert 'ro' in root_mount.split()[3].split(','), root_mount
for path in (canary, '/home/paul', '/root/.ssh', '/var/run/docker.sock',
             '/run/docker.sock', '/run/user/1000/bus'):
    try:
        assert not pathlib.Path(path).exists(), path
    except PermissionError:
        pass
try:
    pathlib.Path('/opt/buzz/base-prompt.md').write_text('changed')
    raise AssertionError('launcher image is writable')
except OSError:
    pass
# There are no host bind mounts or host PID/network namespaces in runtime_args.
for proc in pathlib.Path('/proc').iterdir():
    if proc.name.isdigit():
        try:
            assert proc.joinpath('status').read_text().split('Uid:\t')[1].split('\n')[0].split()[0] != '1000'
        except FileNotFoundError:
            pass
repo = pathlib.Path('/workspace/REPOS/pilot')
repo.mkdir(parents=True)
subprocess.run(['git', 'init', '-q', str(repo)], check=True)
subprocess.run(['git', '-C', str(repo), 'config', 'user.name', 'Isolation probe'], check=True)
subprocess.run(['git', '-C', str(repo), 'config', 'user.email', 'probe@example.invalid'], check=True)
repo.joinpath('README.md').write_text('isolated repository\n')
subprocess.run(['git', '-C', str(repo), 'add', 'README.md'], check=True)
subprocess.run(['git', '-C', str(repo), 'commit', '-qm', 'Verify isolated repository writes'], check=True)
# Exercise the actual MCP shell, which must inherit the worker's restrictions.
p = subprocess.Popen(['/opt/buzz/bin/buzz-dev-mcp'], stdin=subprocess.PIPE,
                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1)
selector = selectors.DefaultSelector()
selector.register(p.stdout, selectors.EVENT_READ)
def call(number, method, params):
    p.stdin.write(json.dumps({'jsonrpc':'2.0','id':number,'method':method,'params':params})+'\n')
    p.stdin.flush()
    deadline = time.monotonic()+25
    while time.monotonic() < deadline:
        if not selector.select(max(0, deadline-time.monotonic())):
            break
        line=p.stdout.readline()
        if not line:
            raise AssertionError('MCP exited early')
        result=json.loads(line)
        if result.get('id') == number:
            assert 'error' not in result, result
            return result['result']
    raise AssertionError('MCP timeout')
try:
    call(1,'initialize',{'protocolVersion':'2024-11-05','capabilities':{},'clientInfo':{'name':'isolation-probe','version':'1'}})
    p.stdin.write(json.dumps({'jsonrpc':'2.0','method':'notifications/initialized'})+'\n');p.stdin.flush()
    result=call(2,'tools/call',{'name':'shell','arguments':{'command':
        'test "$(id -u)" = 61021 && test ! -e '+canary+
        ' && test ! -e /home/paul && test ! -S /var/run/docker.sock'+
        ' && git -C /workspace/REPOS/pilot status --porcelain && echo MCP_BOUNDARY_PASS'}})
    assert not result.get('isError'), result
    assert 'MCP_BOUNDARY_PASS' in json.dumps(result), result
finally:
    p.terminate()
    try: p.wait(timeout=5)
    except subprocess.TimeoutExpired: p.kill();p.wait()
print(json.dumps({'uid':os.getuid(),'host_files':'blocked','host_processes':'hidden',
                  'launcher_changes':'blocked','docker_control':'absent',
                  'repo_commit':'passed','real_mcp_shell':'passed'}))
'''
            try:
                args = pilot.runtime_args(image, name, volume, entrypoint="python3", network="none")
                args += ["--interactive", image, "-", marker.name]
                code = code.replace("61021", str(pilot.UID))
                result = subprocess.run(["docker", *args], input=code, text=True,
                                        capture_output=True, timeout=80)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                report = json.loads(result.stdout)
                self.assertEqual(report["real_mcp_shell"], "passed")
                print(json.dumps(report, sort_keys=True))
            finally:
                subprocess.run(["docker", "rm", "--force", name], capture_output=True, timeout=20)
                subprocess.run(["docker", "volume", "rm", volume], check=True,
                               capture_output=True, timeout=20)


if __name__ == "__main__":
    unittest.main()

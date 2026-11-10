#!/usr/bin/env python3
"""Host-side controller for one isolated buzz-agent runtime (never a worker tool)."""
import argparse
from contextlib import ExitStack
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import time
import uuid
from urllib.parse import urlsplit

from relay_tunnel import Tunnel
import workspace

NAME = "buzz-isolated-gemma"
VOLUME = NAME + "-workspace"
LABEL = "buzz.isolation.pilot=gemma"
AGENT = "gemma"
DISPLAY_NAME = "Gemma"
UID = 61021
ORIGINAL_SERVICE = "buzz-acp@gemma.service"
PROFILES = {
    "gemma": (61021, "Gemma"), "fizz": (61022, "Fizz"),
    "bolt": (61023, "Bolt"), "bumble": (61024, "Bumble"),
    "deep": (61025, "DeepSeek V4.1 Flash"), "dew": (61026, "Dew"),
    "glm": (61027, "GLM"), "glm-flash": (61028, "GLM 5.3 Flash"),
    "honey": (61029, "Honey"), "kimi": (61030, "Kimi K3"),
    "lightning": (61031, "Lightning"), "nectar": (61032, "Nectar"),
    "nora": (61033, "Nora"), "north": (61034, "North"),
    "omni": (61035, "Omni"), "spark": (61036, "Spark"),
    "venice": (61037, "Venice"),
    "max": (61038, "Max"),
    "m5": (61039, "m5"),
}
WORKSPACE_ROOT = Path('/home/paul/buzz/nests')


def configure(agent):
    """Select an existing pilot or a successfully provisioned private profile."""
    if agent in PROFILES:
        uid, display_name = PROFILES[agent]
    else:
        import provision
        record = provision.load_profile(agent)
        uid, display_name = record['uid'], agent
    configure_profile(agent, uid, display_name)


def configure_profile(agent, uid, display_name):
    """Set controller names after the caller validates or reserves the profile."""
    global AGENT, DISPLAY_NAME, NAME, VOLUME, LABEL, UID, ORIGINAL_SERVICE
    AGENT = agent
    UID, DISPLAY_NAME = uid, display_name
    NAME = "buzz-isolated-" + agent
    VOLUME = NAME + "-workspace"
    LABEL = "buzz.isolation.pilot=" + agent
    ORIGINAL_SERVICE = "buzz-acp@" + agent + ".service"

# No ambient host environment, proxy configuration, remote MCP or SSH is forwarded.
FORWARDED = frozenset({
    "BUZZ_PRIVATE_KEY", "BUZZ_AUTH_TAG", "BUZZ_RELAY_URL", "BUZZ_ACP_PUBKEY",
    "BUZZ_ACP_AGENT_OWNER", "BUZZ_ACP_DISPLAY_NAME", "BUZZ_ACP_SESSION_TITLE",
    "BUZZ_ACP_AGENTS", "BUZZ_ACP_LAZY_POOL", "BUZZ_ACP_RESPOND_TO",
    "BUZZ_ACP_IDLE_TIMEOUT", "BUZZ_ACP_MAX_TURN_DURATION",
    "BUZZ_ACP_MAX_TURNS_PER_SESSION", "BUZZ_ACP_SYSTEM_PROMPT",
    "BUZZ_AGENT_PROVIDER", "OPENAI_COMPAT_BASE_URL", "OPENAI_COMPAT_MODEL",
    "OPENAI_COMPAT_API_KEY", "OPENAI_COMPAT_API",
    "BUZZ_ACP_CONTEXT_MESSAGE_LIMIT", "BUZZ_ACP_TEAM_INSTRUCTIONS",
    "BUZZ_ACP_RESPOND_TO_ALLOWLIST", "OPENROUTER_API_KEY",
    "OPENROUTER_BASE_URL", "OPENROUTER_MODEL", "BUZZ_AGENT_MODEL",
    "BUZZ_AGENT_THINKING_EFFORT", "BUZZ_AGENT_MAX_OUTPUT_TOKENS",
    "BUZZ_AGENT_MAX_CONTEXT_TOKENS", "BUZZ_AGENT_REQUIRE_REPLY",
})


def run(args, **kwargs):
    return subprocess.run(args, check=True, timeout=kwargs.pop("timeout", 120), **kwargs)


def docker(args, **kwargs):
    return run(["docker", *args], **kwargs)


def image_id(value):
    if not re.fullmatch(r"sha256:[a-f0-9]{64}", value):
        raise ValueError("Use an immutable local image ID (sha256:<64 hex>)")
    return value


def runtime_args(image, name=None, volume=None, entrypoint=None, network="bridge", ssh=False, desktop=False):
    """The same confinement arguments are used for probes and the live harness."""
    image_id(image)
    name = name or NAME
    volume = volume or VOLUME
    args = ["run", "--rm", "--name", name, "--label", LABEL, "--init",
            "--user", f"{UID}:{UID}", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges:true", "--read-only",
            "--pids-limit", "256", "--memory", "2g", "--cpus", "2",
            "--network", network,
            "--sysctl", "net.ipv4.ip_unprivileged_port_start=0",
            "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=128m,mode=1777",
            "--tmpfs", "/run:rw,noexec,nosuid,nodev,size=16m,mode=755",
            "--mount", f"type=volume,source={volume},target=/workspace",
            "--workdir", "/workspace", "--log-driver", "none"]
    if AGENT == "max":
        # Existing Git worktrees store absolute paths to Max's original nest.
        args += ["--mount", f"type=volume,source={volume},target={WORKSPACE_ROOT / AGENT},volume-nocopy"]
    if ssh:
        if AGENT != "fizz":
            raise ValueError("Only the Fizz pilot has an approved SSH credential mount")
        args += ["--mount", "type=volume,source=buzz-isolated-fizz-ssh,target=/run/fizz-ssh,readonly"]
    if desktop:
        if AGENT != "fizz" or not ssh:
            raise ValueError("Desktop is provisioned only for the Fizz SSH pilot")
        args += ["--mount", "type=volume,source=buzz-isolated-fizz-desktop,target=/run/fizz-desktop,readonly"]
    if entrypoint:
        args += ["--entrypoint", entrypoint]
    return args


def build(context, binary_dir, prompt, ag_host=None, known_hosts=None):
    """Build context intentionally contains no environment files or home mounts."""
    if AGENT == "fizz" and (not ag_host or not known_hosts):
        raise ValueError("Fizz build requires a pinned ag host and public known-hosts file")
    if ag_host:
        ipaddress.ip_address(ag_host)
    context = Path(context)
    context.mkdir(parents=True, exist_ok=False)
    (context / "bin").mkdir()
    (context / "ssh").mkdir()
    config = ""
    pins = ""
    if AGENT == "fizz":
        pins = Path(known_hosts).read_text()
        records = [line.split() for line in pins.splitlines() if line.strip()]
        if len(records) != 1 or len(records[0]) != 3 or records[0][:2] != ["ag", "ssh-ed25519"]:
            raise ValueError("Expected exactly one public ag Ed25519 host-key record")
        config = f"""Host *
    IdentityAgent none
    IdentitiesOnly yes
    BatchMode yes
    PasswordAuthentication no
    KbdInteractiveAuthentication no
    ForwardAgent no
    ClearAllForwardings yes
    StrictHostKeyChecking yes
    UserKnownHostsFile /opt/buzz/ssh/known_hosts
    GlobalKnownHostsFile /dev/null
    ControlMaster no
Host ag
    HostName {ag_host}
    User paul
    HostKeyAlias ag
    IdentityFile /run/fizz-ssh/id_ed25519
"""
    (context / "ssh/config").write_text(config)
    (context / "ssh/known_hosts").write_text(pins)
    hashes = {}
    for name in ("buzz-acp", "buzz-agent", "buzz-dev-mcp", "buzz"):
        source = Path(binary_dir) / name
        if not source.is_file() or not os.access(source, os.X_OK):
            raise ValueError(f"Missing executable: {source}")
        destination = context / "bin" / name
        shutil.copyfile(source, destination)
        hashes[name] = hashlib.sha256(destination.read_bytes()).hexdigest()
    shutil.copyfile(prompt, context / "base-prompt.md")
    shutil.copyfile(Path(__file__).with_name("Dockerfile"), context / "Dockerfile")
    for name in ("contained.py", "relay_tunnel.py"):
        shutil.copyfile(Path(__file__).with_name(name), context / name)
    (context / "manifest.json").write_text(json.dumps(hashes, indent=2) + "\n")
    docker(["build", "--build-arg", f"WORKER_UID={UID}", "--tag", NAME + ":pilot", str(context)], timeout=600)
    result = docker(["image", "inspect", "--format", "{{.Id}}", NAME + ":pilot"],
                    capture_output=True, text=True)
    print(result.stdout.strip())
    print(json.dumps(hashes, sort_keys=True))


def start(image):
    if AGENT not in PROFILES:
        import provision
        record = provision.load_profile(AGENT)
        if record['image'] != image:
            raise ValueError('Provisioned image differs from its reserved profile')
        if record.get('family') == 'claude':
            import claude_profile
            return claude_profile.start(sys.modules[__name__], image)
    required = {"BUZZ_PRIVATE_KEY", "BUZZ_RELAY_URL"}
    provider_name = os.environ.get("BUZZ_AGENT_PROVIDER")
    if provider_name == "openai":
        required |= {"OPENAI_COMPAT_API_KEY", "OPENAI_COMPAT_BASE_URL", "OPENAI_COMPAT_MODEL"}
    elif provider_name == "openrouter":
        required |= {"OPENROUTER_API_KEY", "OPENROUTER_MODEL"}
    else:
        raise ValueError("Pilot supports only the existing openai/openrouter providers")
    if not all(os.environ.get(key) for key in required):
        raise ValueError("Pilot requires its own complete provider environment")
    desktop_profile = docker(["image", "inspect", image, "--format",
                              '{{index .Config.Labels "buzz.isolation.desktop"}}'],
                             capture_output=True, text=True).stdout.strip()
    args = runtime_args(image, ssh=(AGENT == "fizz"), desktop=(desktop_profile == "fizz"))
    if AGENT == 'max':
        app_profile = docker(['image', 'inspect', image, '--format',
                              '{{index .Config.Labels "buzz.isolation.max_app"}}'],
                             capture_output=True, text=True).stdout.strip()
        if app_profile != '1':
            raise ValueError("Max requires its existing Node application in the image")
        args += ['--publish', '8080:8080']
    # Docker's default resolver cannot resolve the host's private/Tailscale DNS.
    # Resolve only the configured relay on the trusted host, retaining its TLS name.
    host = urlsplit(os.environ["BUZZ_RELAY_URL"]).hostname
    if not host:
        raise ValueError("Relay URL must have a hostname")
    address = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)[0][4][0]
    args += ["--add-host", f"{host}:127.0.0.1"]
    # Public DNS is intermittent on this bridge, and MagicDNS is host-only.
    # Resolve the configured provider on the host without changing its TLS name
    # or exposing a general-purpose DNS/host-network capability to the worker.
    provider_url = urlsplit(os.environ.get(
        "OPENROUTER_BASE_URL" if provider_name == "openrouter" else "OPENAI_COMPAT_BASE_URL",
        "https://openrouter.ai/api/v1"))
    if provider_url.hostname and provider_url.hostname != host:
        try:
            ipaddress.ip_address(provider_url.hostname)
        except ValueError:
            provider_address = socket.getaddrinfo(
                provider_url.hostname, provider_url.port or 443,
                family=socket.AF_INET, type=socket.SOCK_STREAM)[0][4][0]
            args += ["--add-host", f"{provider_url.hostname}:{provider_address}"]
    # Pass variable names, never secret values in the process argument list.
    for key in sorted(key for key in FORWARDED if os.environ.get(key)):
        args += ["--env", key]
    bridge = docker(["network", "inspect", "bridge", "--format",
                     "{{(index .IPAM.Config 0).Gateway}}"], capture_output=True, text=True).stdout.strip()
    # The host's Tailscale listener rejects connections arriving on docker0.
    # This fixed-destination TCP hop is bound only to docker0, never a public IP.
    # A second hop inside the worker retains the original relay URL and TLS name.
    with ExitStack() as cleanup:
        tunnel = cleanup.enter_context(Tunnel((bridge, 0), (address, 443)))
        port = tunnel.start()
        cleanup.callback(tunnel.shutdown)
        args += ["--env", "BUZZ_PILOT_RELAY_TARGET=" + json.dumps([bridge, port])]
        if AGENT == "fizz":
            provider = urlsplit(os.environ["OPENAI_COMPAT_BASE_URL"])
            if (provider.scheme, provider.hostname, provider.port) != ("http", "127.0.0.1", 8100):
                raise ValueError("Fizz pilot requires its existing local model service")
            model_tunnel = cleanup.enter_context(Tunnel((bridge, 0), ("127.0.0.1", 8100)))
            model_port = model_tunnel.start()
            cleanup.callback(model_tunnel.shutdown)
            args += ["--env", "BUZZ_PILOT_MODEL_TARGET=" + json.dumps([bridge, model_port])]
        return subprocess.call(["docker", *args, image])


def systemctl(*args, timeout=120):
    env = dict(os.environ)
    env["XDG_RUNTIME_DIR"] = f"/run/user/{os.getuid()}"
    env["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path=/run/user/{os.getuid()}/bus"
    return run(["systemctl", "--user", *args], env=env, capture_output=True, text=True, timeout=timeout)


def migrate_workspace(image, source, timeout=900):
    """Called only after the old harness is stopped; no host bind is mounted."""
    expected = workspace.digest(source)
    helper = NAME + '-copy-' + uuid.uuid4().hex[:12]
    producer = subprocess.Popen(['tar', '-C', str(source), '-cf', '-', '.'],
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        code = Path(workspace.__file__).read_text()
        args = ['run', '--rm', '--name', helper, '--interactive', '--network', 'none', '--user', '0',
                '--read-only', '--cap-drop', 'ALL', '--cap-add', 'CHOWN', '--cap-add', 'FOWNER',
                '--security-opt', 'no-new-privileges', '--pids-limit', '64', '--memory', '512m',
                '--mount', f'type=volume,source={VOLUME},target=/workspace',
                '--entrypoint', 'python3', image, '-c', code, str(UID)]
        result = docker(args, stdin=producer.stdout, capture_output=True, text=True, timeout=timeout)
        producer.stdout.close()
        if producer.wait(timeout=10) != 0:
            raise RuntimeError('Workspace archive failed; original kept for rollback')
        copied = json.loads(result.stdout)
        if copied != expected or workspace.digest(source) != expected:
            raise RuntimeError('Workspace changed during migration or copy digest differs')
        print(json.dumps({'workspace_copy': 'verified', **copied}))
    finally:
        # Killing a timed-out Docker client does not stop its container. Remove
        # only this disposable helper; keep its private volume and the source.
        subprocess.run(['docker', 'rm', '--force', helper],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
        producer.stdout.close()
        if producer.poll() is None:
            producer.terminate()
            producer.wait(timeout=10)


def verify_stopped_snapshot(image, source_env, snapshot):
    """Bind an explicitly reviewed stopped-agent start to its prepared state."""
    if AGENT not in {'deep', 'nectar'}:
        raise ValueError('Stopped-agent startup is prepared only for Deep and Nectar')
    record = json.loads(Path(snapshot).read_text())
    if (record.get('agent') != AGENT or record.get('image') != image or
            record.get('environment_file_sha256') != hashlib.sha256(source_env.read_bytes()).hexdigest()):
        raise ValueError('Stopped-agent snapshot does not match agent, image or settings')
    for property_name, expected in [('ActiveState', 'inactive'), ('UnitFileState', 'disabled'), ('MainPID', '0')]:
        actual = systemctl('show', ORIGINAL_SERVICE, '--property=' + property_name, '--value').stdout.strip()
        if actual != expected:
            raise ValueError('Original stopped-agent service changed; leave it untouched')
    for property_name, expected in [('ActiveState', 'inactive'), ('MainPID', '0')]:
        actual = systemctl('show', NAME + '.service', '--property=' + property_name, '--value').stdout.strip()
        if actual != expected:
            raise ValueError('Stopped-agent replacement already has an active service')
    if workspace.digest(workspace.validate(WORKSPACE_ROOT / AGENT)) != record.get('workspace'):
        raise ValueError('Stopped-agent source workspace changed after preparation')
    for query in [['ps', '-aq', '--filter', 'name=^/' + NAME + '$'],
                  ['ps', '-q', '--filter', 'volume=' + VOLUME]]:
        if docker(query, capture_output=True, text=True).stdout.strip():
            raise ValueError('Stopped-agent container name or prepared workspace is already in use')
    # Inspect only the existing private volume, without a host mount or network.
    docker(['volume', 'inspect', VOLUME], stdout=subprocess.DEVNULL)
    code = Path(workspace.__file__).read_text().split("if __name__ == '__main__':")[0]
    code += "\nimport json\nvalidate('/workspace')\nprint(json.dumps(digest('/workspace')))\n"
    result = docker(['run', '--rm', '--network', 'none', '--read-only', '--cap-drop', 'ALL',
                     '--security-opt', 'no-new-privileges', '--user', f'{UID}:{UID}',
                     '--pids-limit', '64', '--memory', '256m', '--mount',
                     f'type=volume,source={VOLUME},target=/workspace,readonly,volume-nocopy',
                     '--entrypoint', 'python3', image, '-c', code], capture_output=True, text=True)
    if json.loads(result.stdout) != record.get('private_workspace'):
        raise ValueError('Prepared stopped-agent workspace changed after review')


def activate(image, source_env, from_stopped=False, snapshot=None):
    image_id(image)
    script = Path(__file__).resolve()
    source_env = Path(source_env).resolve(strict=True)
    if from_stopped != (snapshot is not None):
        raise ValueError('Stopped-agent startup requires both --from-stopped and --snapshot')
    if from_stopped:
        verify_stopped_snapshot(image, source_env, snapshot)
    source_workspace = None
    if AGENT not in {'gemma', 'fizz'}:
        source_workspace = workspace.validate(WORKSPACE_ROOT / AGENT)
    # Fixed pilot scope; never accepts a user-provided service, mount, or command.
    if any(c in str(p) for p in (script, source_env) for c in ' \t\n\r%"\\'):
        raise ValueError("Unsupported character in unit path")
    unit_dir = Path.home() / ".config/systemd/user"
    unit = unit_dir / (NAME + ".service")
    if from_stopped and (unit.exists() or unit.is_symlink()):
        raise ValueError('Preserve and inspect the existing stopped-agent replacement unit')
    if unit.exists():
        if unit.is_symlink() or f"Description=Isolated {DISPLAY_NAME} pilot" not in unit.read_text():
            raise ValueError("Refusing to overwrite an unknown pilot unit")
        if systemctl("show", NAME + ".service", "--property=ActiveState", "--value").stdout.strip() != "inactive":
            raise ValueError("Pilot unit already exists and is not stopped")
    state = systemctl("show", ORIGINAL_SERVICE, "--property=ActiveState", "--value").stdout.strip()
    if state != ("inactive" if from_stopped else "active"):
        raise ValueError(f"Original {DISPLAY_NAME} service does not match the selected startup mode")
    docker(["image", "inspect", image], stdout=subprocess.DEVNULL)
    actual_user = docker(["image", "inspect", image, "--format", "{{.Config.User}}"],
                         capture_output=True, text=True).stdout.strip()
    if actual_user != f'{UID}:{UID}':
        raise ValueError('Image identity does not match the selected agent profile')
    docker(["volume", "create", "--label", LABEL, VOLUME], capture_output=True)
    # EnvironmentFile overrides defaults, including settings added by Hive later.
    defaults = ' '.join(key + '=' for key in sorted(FORWARDED))
    unit.write_text(f'''[Unit]
Description=Isolated {DISPLAY_NAME} pilot (harness and every child tool)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
Environment=PATH=/usr/local/bin:/usr/bin:/bin
Environment={defaults}
EnvironmentFile={source_env}
ExecStart=/usr/bin/python3 "{script}" --agent {AGENT} run --image {image}
ExecStop=/usr/bin/docker stop --time 25 {NAME}
Restart=on-failure
RestartSec=15
TimeoutStopSec=45
UMask=0077
StandardOutput=null
StandardError=journal

[Install]
WantedBy=default.target
''')
    systemctl("daemon-reload")
    # Stop the old identity before the replacement ever connects to the relay.
    try:
        if not from_stopped:
            systemctl("disable", "--now", ORIGINAL_SERVICE)
        if systemctl("show", ORIGINAL_SERVICE, "--property=MainPID", "--value").stdout.strip() != "0":
            raise RuntimeError(f"Old {DISPLAY_NAME} still has a MainPID; refusing duplicate identity")
        if from_stopped:
            verify_stopped_snapshot(image, source_env, snapshot)
        elif source_workspace is not None:
            migrate_workspace(image, source_workspace)
        systemctl("enable", "--now", NAME + ".service")
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            state = subprocess.run(["docker", "inspect", "--format", "{{.State.Running}}", NAME],
                                   capture_output=True, text=True, timeout=10)
            if state.returncode == 0 and state.stdout.strip() == "true":
                time.sleep(2)
                docker(["exec", NAME, "test", "-x", "/opt/buzz/bin/buzz-acp"],
                       stdout=subprocess.DEVNULL)
                break
            time.sleep(1)
        else:
            raise RuntimeError("Pilot did not become a running container")
        systemctl("is-active", NAME + ".service")
    except (subprocess.SubprocessError, RuntimeError, ValueError, OSError):
        systemctl("disable", "--now", NAME + ".service")
        remaining = docker(["ps", "--filter", "name=^/" + NAME + "$", "--format", "{{.ID}}"],
                           capture_output=True, text=True)
        if remaining.stdout.strip():
            raise RuntimeError("Pilot failed but is still running; original kept stopped")
        if not from_stopped:
            systemctl("enable", "--now", ORIGINAL_SERVICE)
        unit.unlink()
        systemctl("daemon-reload")
        raise
    print("Isolated pilot enabled; original service stopped and disabled")


def rollback(keep_original_stopped=False):
    if keep_original_stopped and AGENT not in {'deep', 'nectar'}:
        raise ValueError('Stopped-agent rollback is prepared only for Deep and Nectar')
    systemctl("disable", "--now", NAME + ".service")
    result = docker(["ps", "--filter", "name=^/" + NAME + "$", "--format", "{{.ID}}"],
                    capture_output=True, text=True)
    if result.stdout.strip():
        raise RuntimeError("Pilot container is still running; refusing duplicate identity")
    if keep_original_stopped:
        for property_name, expected in [('ActiveState', 'inactive'), ('UnitFileState', 'disabled'), ('MainPID', '0')]:
            actual = systemctl('show', ORIGINAL_SERVICE, '--property=' + property_name, '--value').stdout.strip()
            if actual != expected:
                raise ValueError('Replacement stopped; original service changed independently')
        print(f'{DISPLAY_NAME} returned to stopped state; private workspace retained')
    else:
        systemctl("enable", "--now", ORIGINAL_SERVICE)
        print(f"Original {DISPLAY_NAME} restored; pilot workspace retained")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent", default="gemma")
    commands = parser.add_subparsers(dest="command", required=True)
    p = commands.add_parser("build")
    p.add_argument("--context", required=True)
    p.add_argument("--binary-dir", required=True)
    p.add_argument("--prompt", required=True)
    p.add_argument("--ag-host")
    p.add_argument("--known-hosts")
    for command in ("run", "activate"):
        p = commands.add_parser(command)
        p.add_argument("--image", required=True, type=image_id)
        if command == "activate":
            p.add_argument("--env-file", required=True)
            p.add_argument("--from-stopped", action="store_true")
            p.add_argument("--snapshot")
    p = commands.add_parser("rollback")
    p.add_argument("--keep-original-stopped", action="store_true")
    p = commands.add_parser("provision")
    p.add_argument("--env-file", required=True)
    p.add_argument("--image", required=True, type=image_id)
    commands.add_parser('claude-login')
    commands.add_parser('claude-activate')
    args = parser.parse_args()
    if args.command in {'claude-login', 'claude-activate'}:
        import claude_profile
        controller = sys.modules[__name__]
        if args.command == 'claude-login':
            claude_profile.login(controller, args.agent)
        else:
            claude_profile.activate(controller, args.agent)
        return
    if args.command == 'provision':
        import provision
        provision.provision(sys.modules[__name__], args.agent, args.env_file, args.image)
        return
    configure(args.agent)
    if args.agent not in PROFILES and args.command != 'run':
        raise ValueError('Provisioned agents support run only; stop/start their isolated service')
    if args.command == "build":
        build(args.context, args.binary_dir, args.prompt, args.ag_host, args.known_hosts)
    elif args.command == "run":
        raise SystemExit(start(args.image))
    elif args.command == "activate":
        activate(args.image, args.env_file, args.from_stopped, args.snapshot)
    else:
        rollback(args.keep_original_stopped)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, subprocess.SubprocessError, RuntimeError) as error:
        # Do not include subprocess environments or arbitrary provider output.
        detail = '; '.join([str(error), *getattr(error, '__notes__', [])])
        print(f"pilot: {type(error).__name__}: {detail}", file=sys.stderr)
        sys.exit(1)

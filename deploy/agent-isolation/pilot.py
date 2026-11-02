#!/usr/bin/env python3
"""Host-side controller for one isolated buzz-agent runtime (never a worker tool)."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import time
from urllib.parse import urlsplit

from relay_tunnel import Tunnel

NAME = "buzz-isolated-gemma"
VOLUME = NAME + "-workspace"
LABEL = "buzz.isolation.pilot=gemma"
# No ambient host environment, proxy configuration, remote MCP or SSH is forwarded.
FORWARDED = frozenset({
    "BUZZ_PRIVATE_KEY", "BUZZ_AUTH_TAG", "BUZZ_RELAY_URL", "BUZZ_ACP_PUBKEY",
    "BUZZ_ACP_AGENT_OWNER", "BUZZ_ACP_DISPLAY_NAME", "BUZZ_ACP_SESSION_TITLE",
    "BUZZ_ACP_AGENTS", "BUZZ_ACP_LAZY_POOL", "BUZZ_ACP_RESPOND_TO",
    "BUZZ_ACP_IDLE_TIMEOUT", "BUZZ_ACP_MAX_TURN_DURATION",
    "BUZZ_ACP_MAX_TURNS_PER_SESSION", "BUZZ_ACP_SYSTEM_PROMPT",
    "BUZZ_AGENT_PROVIDER", "OPENAI_COMPAT_BASE_URL", "OPENAI_COMPAT_MODEL",
    "OPENAI_COMPAT_API_KEY", "OPENAI_COMPAT_API",
})


def run(args, **kwargs):
    return subprocess.run(args, check=True, timeout=kwargs.pop("timeout", 120), **kwargs)


def docker(args, **kwargs):
    return run(["docker", *args], **kwargs)


def image_id(value):
    if not re.fullmatch(r"sha256:[a-f0-9]{64}", value):
        raise ValueError("Use an immutable local image ID (sha256:<64 hex>)")
    return value


def runtime_args(image, name=NAME, volume=VOLUME, entrypoint=None, network="bridge"):
    """The same confinement arguments are used for probes and the live harness."""
    image_id(image)
    args = ["run", "--rm", "--name", name, "--label", LABEL, "--init",
            "--user", "61021:61021", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges:true", "--read-only",
            "--pids-limit", "256", "--memory", "2g", "--cpus", "2",
            "--network", network,
            "--sysctl", "net.ipv4.ip_unprivileged_port_start=0",
            "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=128m,mode=1777",
            "--tmpfs", "/run:rw,noexec,nosuid,nodev,size=16m,mode=755",
            "--mount", f"type=volume,source={volume},target=/workspace",
            "--workdir", "/workspace", "--log-driver", "none"]
    if entrypoint:
        args += ["--entrypoint", entrypoint]
    return args


def build(context, binary_dir, prompt):
    """Build context intentionally contains no environment files or home mounts."""
    context = Path(context)
    context.mkdir(parents=True, exist_ok=False)
    (context / "bin").mkdir()
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
    docker(["build", "--tag", NAME + ":pilot", str(context)], timeout=600)
    result = docker(["image", "inspect", "--format", "{{.Id}}", NAME + ":pilot"],
                    capture_output=True, text=True)
    print(result.stdout.strip())
    print(json.dumps(hashes, sort_keys=True))


def start(image):
    required = {"BUZZ_PRIVATE_KEY", "BUZZ_RELAY_URL", "OPENAI_COMPAT_API_KEY",
                "OPENAI_COMPAT_BASE_URL", "OPENAI_COMPAT_MODEL"}
    if not required.issubset(os.environ) or os.environ.get("BUZZ_AGENT_PROVIDER") != "openai":
        raise ValueError("Pilot requires its own complete OpenAI-compatible environment")
    args = runtime_args(image)
    # Docker's default resolver cannot resolve the host's private/Tailscale DNS.
    # Resolve only the configured relay on the trusted host, retaining its TLS name.
    host = urlsplit(os.environ["BUZZ_RELAY_URL"]).hostname
    if not host:
        raise ValueError("Relay URL must have a hostname")
    address = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)[0][4][0]
    args += ["--add-host", f"{host}:127.0.0.1"]
    # Pass variable names, never secret values in the process argument list.
    for key in sorted(FORWARDED.intersection(os.environ)):
        args += ["--env", key]
    bridge = docker(["network", "inspect", "bridge", "--format",
                     "{{(index .IPAM.Config 0).Gateway}}"], capture_output=True, text=True).stdout.strip()
    # The host's Tailscale listener rejects connections arriving on docker0.
    # This fixed-destination TCP hop is bound only to docker0, never a public IP.
    # A second hop inside the worker retains the original relay URL and TLS name.
    with Tunnel((bridge, 0), (address, 443)) as tunnel:
        port = tunnel.start()
        args += ["--env", "BUZZ_PILOT_RELAY_TARGET=" + json.dumps([bridge, port])]
        try:
            return subprocess.call(["docker", *args, image])
        finally:
            tunnel.shutdown()


def systemctl(*args):
    env = dict(os.environ)
    env["XDG_RUNTIME_DIR"] = f"/run/user/{os.getuid()}"
    env["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path=/run/user/{os.getuid()}/bus"
    return run(["systemctl", "--user", *args], env=env, capture_output=True, text=True)


def activate(image, source_env):
    image_id(image)
    script = Path(__file__).resolve()
    source_env = Path(source_env).resolve(strict=True)
    # Fixed pilot scope; never accepts a user-provided service, mount, or command.
    if any(c in str(p) for p in (script, source_env) for c in ' \t\n\r%"\\'):
        raise ValueError("Unsupported character in unit path")
    unit_dir = Path.home() / ".config/systemd/user"
    unit = unit_dir / (NAME + ".service")
    if unit.exists():
        if unit.is_symlink() or "Description=Isolated Gemma pilot" not in unit.read_text():
            raise ValueError("Refusing to overwrite an unknown pilot unit")
        if systemctl("show", NAME + ".service", "--property=ActiveState", "--value").stdout.strip() != "inactive":
            raise ValueError("Pilot unit already exists and is not stopped")
    state = systemctl("show", "buzz-acp@gemma.service", "--property=ActiveState", "--value").stdout.strip()
    if state != "active":
        raise ValueError("Expected the original Gemma service to be active before migration")
    docker(["image", "inspect", image], stdout=subprocess.DEVNULL)
    docker(["volume", "create", "--label", LABEL, VOLUME], capture_output=True)
    unit.write_text(f'''[Unit]
Description=Isolated Gemma pilot (harness and every child tool)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
Environment=PATH=/usr/local/bin:/usr/bin:/bin
EnvironmentFile={source_env}
ExecStart=/usr/bin/python3 "{script}" run --image {image}
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
        systemctl("disable", "--now", "buzz-acp@gemma.service")
        if systemctl("show", "buzz-acp@gemma.service", "--property=MainPID", "--value").stdout.strip() != "0":
            raise RuntimeError("Old Gemma still has a MainPID; refusing duplicate identity")
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
    except (subprocess.SubprocessError, RuntimeError):
        systemctl("disable", "--now", NAME + ".service")
        remaining = docker(["ps", "--filter", "name=^/" + NAME + "$", "--format", "{{.ID}}"],
                           capture_output=True, text=True)
        if remaining.stdout.strip():
            raise RuntimeError("Pilot failed but is still running; original kept stopped")
        systemctl("enable", "--now", "buzz-acp@gemma.service")
        unit.unlink()
        systemctl("daemon-reload")
        raise
    print("Isolated pilot enabled; original service stopped and disabled")


def rollback():
    systemctl("disable", "--now", NAME + ".service")
    result = docker(["ps", "--filter", "name=^/" + NAME + "$", "--format", "{{.ID}}"],
                    capture_output=True, text=True)
    if result.stdout.strip():
        raise RuntimeError("Pilot container is still running; refusing duplicate identity")
    systemctl("enable", "--now", "buzz-acp@gemma.service")
    print("Original Gemma restored; pilot workspace retained")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    p = commands.add_parser("build")
    p.add_argument("--context", required=True)
    p.add_argument("--binary-dir", required=True)
    p.add_argument("--prompt", required=True)
    for command in ("run", "activate"):
        p = commands.add_parser(command)
        p.add_argument("--image", required=True, type=image_id)
        if command == "activate":
            p.add_argument("--env-file", required=True)
    commands.add_parser("rollback")
    args = parser.parse_args()
    if args.command == "build":
        build(args.context, args.binary_dir, args.prompt)
    elif args.command == "run":
        raise SystemExit(start(args.image))
    elif args.command == "activate":
        activate(args.image, args.env_file)
    else:
        rollback()


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, subprocess.SubprocessError, RuntimeError) as error:
        # Do not include subprocess environments or arbitrary provider output.
        print(f"pilot: {type(error).__name__}: {error}", file=sys.stderr)
        sys.exit(1)

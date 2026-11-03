#!/usr/bin/env python3
"""Trusted host builder for Fizz's existing desktop client; never stages keys."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess


def build(context, base, node, compiled, pin_file):
    if not re.fullmatch(r"sha256:[a-f0-9]{64}", base):
        raise ValueError("base must be an immutable local image ID")
    if not re.fullmatch(r"node@sha256:[a-f0-9]{64}", node):
        raise ValueError("node must be digest pinned")
    profile = subprocess.run(["docker", "image", "inspect", base, "--format", "{{.Config.User}}"],
                             check=True, capture_output=True, text=True, timeout=20).stdout.strip()
    if profile != "61022:61022":
        raise ValueError("only the existing Fizz image can be extended")
    # BuildKit treats a bare sha256 ID in FROM as a registry repository.
    # Give this exact local object a content-derived name, check it again
    # after the build, and retain the immutable ID in the build manifest.
    local_base = "buzz-isolated-fizz:base-" + base.split(':')[1]
    subprocess.run(["docker", "tag", base, local_base], check=True, timeout=20)
    pins = Path(pin_file).read_text()
    records = [line.split() for line in pins.splitlines() if line.strip()]
    if len(records) != 1 or len(records[0]) != 3 or records[0][:2] != ["hive-desk", "ssh-ed25519"]:
        raise ValueError("provide exactly one trusted public hive-desk host pin")
    context = Path(context)
    context.mkdir(exist_ok=False)
    desktop = context / "desktop"
    (desktop / "mcp").mkdir(parents=True)
    for name in ["protocol.js", "streams.js", "types.js", *[f"mcp/{n}.js" for n in
                 ["main", "client", "server", "tools", "png"]]]:
        shutil.copyfile(Path(compiled) / "src/desktop" / name, desktop / name)
    (desktop / "package.json").write_text('{"type":"module"}\n')
    (context / "desktop-known-hosts").write_text(pins)
    (context / "desktop-ssh.conf").write_text("""Host hive-desk
    HostName 10.0.64.160
    User hive-desktop
    HostKeyAlias hive-desk
    IdentityFile /run/fizz-desktop/id_ed25519
    UserKnownHostsFile /opt/buzz/desktop-known-hosts
""")
    (context / "fizz-mcp").write_text("#!/bin/sh\nexec python3 /opt/buzz/mcp_merge.py /opt/buzz/fizz-merge.json\n")
    (context / "desktop-mcp").write_text("""#!/bin/sh
export DESKTOP_SSH_TARGET=hive-desktop@hive-desk
export DESKTOP_SSH_KEY=/run/fizz-desktop/id_ed25519
exec /usr/local/bin/node /opt/buzz/desktop/mcp/main.js
""")
    (context / "fizz-merge.json").write_text(json.dumps({"servers": [
        {"name": "dev", "command": "/opt/buzz/bin/buzz-dev-mcp"},
        {"name": "desktop", "command": "/opt/buzz/bin/desktop-mcp"},
    ]}, indent=2) + "\n")
    here = Path(__file__).parent
    shutil.copyfile(here / "mcp_merge.py", context / "mcp_merge.py")
    shutil.copyfile(here / "Dockerfile.desktop", context / "Dockerfile")
    manifest = {str(p.relative_to(context)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in sorted(context.rglob('*')) if p.is_file()}
    (context / "manifest.json").write_text(json.dumps({"base": base, "node": node,
                                                    "files": manifest}, indent=2) + "\n")
    subprocess.run(["docker", "build", "--build-arg", "BASE_IMAGE=" + local_base,
                    "--build-arg", "NODE_IMAGE=" + node, "--tag",
                    "buzz-isolated-fizz:desktop-20261103", str(context)], check=True, timeout=600)
    after = subprocess.run(["docker", "image", "inspect", local_base, "--format", "{{.Id}}"],
                           check=True, capture_output=True, text=True, timeout=20).stdout.strip()
    if after != base:
        raise RuntimeError("local base reference changed during build")
    subprocess.run(["docker", "image", "inspect", "buzz-isolated-fizz:desktop-20261103",
                    "--format", "{{.Id}}"], check=True, timeout=20)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for option in ["context", "base", "node", "compiled", "pin-file"]:
        parser.add_argument("--" + option, required=True)
    args = parser.parse_args()
    build(args.context, args.base, args.node, args.compiled, args.pin_file)

#!/usr/bin/env python3
"""Stage installed native runtime code, never a home directory or credential."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess

import pilot


def reprofile(context, image, family, uid):
    """Reuse already verified runtime code; change only the fixed worker UID."""
    pilot.image_id(image)
    actual_family = pilot.docker(['image', 'inspect', image, '--format',
                                  '{{index .Config.Labels "buzz.isolation.native"}}'],
                                 capture_output=True, text=True).stdout.strip()
    if actual_family != family:
        raise ValueError('Cannot change runtime family while changing identity')
    context = Path(context)
    context.mkdir(exist_ok=False)
    tag = 'buzz-native-runtime:' + image.split(':')[1]
    pilot.docker(['tag', image, tag])
    (context / 'Dockerfile').write_text(f'''FROM {tag}
USER 0
RUN /usr/sbin/groupmod --gid {uid} native-worker \\
    && /usr/sbin/usermod --uid {uid} --gid {uid} native-worker \\
    && chown {uid}:{uid} /home/paul
USER {uid}:{uid}
''')
    (context / 'manifest.json').write_text(json.dumps({'runtime': image, 'uid': uid,
                                                     'family': family}, indent=2) + '\n')
    destination = f'buzz-native-{family}:uid-{uid}'
    pilot.docker(['build', '--tag', destination, str(context)], timeout=300)
    after = pilot.docker(['image', 'inspect', tag, '--format', '{{.Id}}'],
                          capture_output=True, text=True).stdout.strip()
    if after != image:
        raise RuntimeError('Runtime image changed during build')
    print(pilot.docker(['image', 'inspect', destination, '--format', '{{.Id}}'],
                       capture_output=True, text=True).stdout.strip())


def build(context, base, node, family, uid, prompt):
    pilot.image_id(base)
    pilot.image_id(node)
    if family not in {'claude', 'codex', 'cursor', 'omp'}:
        raise ValueError('Unsupported native runtime')
    context = Path(context)
    context.mkdir(exist_ok=False)
    local = Path('/home/paul/.local')
    runtime = context / 'runtime'
    runtime.mkdir()
    binaries = context / 'bin'
    binaries.mkdir()
    for name in ['buzz-acp', 'buzz', 'buzz-dev-mcp']:
        shutil.copy2(local / 'bin' / name, binaries / name)
    if family in {'claude', 'codex'}:
        package = family + '-agent-acp' if family == 'claude' else 'codex-acp'
        shutil.copytree(local / 'lib/node_modules/@agentclientprotocol' / package,
                        runtime / 'adapter')
        command = 'node /opt/buzz/native/adapter/dist/index.js'
        if family == 'claude':
            shutil.copy2(local / 'bin/claude', runtime / 'claude')
            command = 'CLAUDE_CODE_EXECUTABLE=/opt/buzz/native/claude ' + command
        else:
            shutil.copytree(local / 'share/buzz-codex-gpt6', runtime / 'codex')
            command = 'CODEX_PATH=/opt/buzz/native/codex/node_modules/.bin/codex ' + command
    elif family == 'cursor':
        source = (local / 'bin/cursor-agent').resolve().parent
        shutil.copytree(source, runtime / 'cursor')
        command = '/opt/buzz/native/cursor/cursor-agent'
    else:
        shutil.copy2(local / 'bin/omp', runtime / 'omp')
        command = '/opt/buzz/native/omp'
    (binaries / 'native-adapter').write_text('#!/bin/sh\nexec ' + command + ' "$@"\n')
    # env assignment before exec is needed for the two Node-based adapters.
    if family in {'claude', 'codex'}:
        (binaries / 'native-adapter').write_text('#!/bin/sh\nexec env ' + command + ' "$@"\n')
    shutil.copyfile(prompt, context / 'base-prompt.md')
    for name in ['contained.py', 'relay_tunnel.py']:
        shutil.copyfile(Path(__file__).with_name(name), context / name)
    base_tag = 'buzz-native-base:' + base.split(':')[1]
    node_tag = 'buzz-native-node:' + node.split(':')[1]
    for image, tag in [(base, base_tag), (node, node_tag)]:
        pilot.docker(['tag', image, tag])
    (context / 'Dockerfile').write_text(f'''FROM {node_tag} AS node
FROM {base_tag}
USER 0
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --chmod=0555 bin/ /opt/buzz/bin/
COPY runtime/ /opt/buzz/native/
COPY --chmod=0444 base-prompt.md contained.py relay_tunnel.py /opt/buzz/
RUN /usr/sbin/groupadd --gid {uid} native-worker && /usr/sbin/useradd --uid {uid} --gid {uid} --home-dir /home/paul --no-create-home native-worker \\
    && mkdir -p /home/paul && chown {uid}:{uid} /home/paul \\
    && find /opt/buzz/native -type d -exec chmod a+rx {{}} + \\
    && node --version
ENV HOME=/home/paul BUZZ_ACP_AGENT_COMMAND=/opt/buzz/bin/native-adapter BUZZ_ACP_AGENT_ARGS="" BUZZ_ACP_MCP_COMMAND=/opt/buzz/bin/buzz-dev-mcp
LABEL buzz.isolation.native="{family}"
USER {uid}:{uid}
''')
    manifest = {str(p.relative_to(context)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in sorted(context.rglob('*')) if p.is_file()}
    (context / 'manifest.json').write_text(json.dumps(
        {'base': base, 'node': node, 'family': family, 'uid': uid, 'files': manifest}, indent=2) + '\n')
    tag = f'buzz-native-{family}:uid-{uid}'
    pilot.docker(['build', '--tag', tag, str(context)], timeout=900)
    for image, tag_ref in [(base, base_tag), (node, node_tag)]:
        actual = pilot.docker(['image', 'inspect', tag_ref, '--format', '{{.Id}}'],
                              capture_output=True, text=True).stdout.strip()
        if actual != image:
            raise RuntimeError('Base image changed during build')
    print(pilot.docker(['image', 'inspect', tag, '--format', '{{.Id}}'],
                       capture_output=True, text=True).stdout.strip())


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for option in ['context', 'base', 'node', 'family', 'prompt']:
        parser.add_argument('--' + option, required=True)
    parser.add_argument('--uid', required=True, type=int)
    args = parser.parse_args()
    build(args.context, args.base, args.node, args.family, args.uid, args.prompt)

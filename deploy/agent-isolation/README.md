# One-agent isolation pilot

This operator deployment runs an existing `buzz-acp` + `buzz-agent` +
`buzz-dev-mcp` installation inside one non-root container. It changes no Buzz
wire protocol and does not rely on the launch-prefix hook being supported by
an older deployed binary. The harness, every worker, MCP server and shell run
inside the same boundary.

## Scope

The fixed pilot is Gemma, with UID/GID 61021, a private named workspace volume,
a read-only image and private PID/mount/network namespaces. No host home,
Docker socket, SSH agent, host D-Bus, privileged capabilities or host repository
is mounted. Repository work starts with one local repo in the private volume;
no shared GitHub or machine-admin credential is supplied. The image is selected
by its immutable ID. The host systemd service supervises the Docker client;
there is no worker-side Docker access. Docker's default seccomp profile remains
in force. Process, CPU and memory limits bound resource use.

This is the first containment step, not keyless execution. The pilot still has
its **own** Buzz and provider credentials and ordinary outbound networking. It
can act with its own Buzz memberships and can send information it can read to
the network. It cannot read other agents' credentials through host paths.
Host administrators and agents still running with host Docker access remain
trusted and can inspect the pilot. Isolating one worker does not isolate the
remaining fleet or protect against a kernel/container-runtime exploit.

A later broker must authenticate each worker and authorize semantic operations,
identities and destinations before signing. Merely offering arbitrary signing
would not enforce those permissions. Audience-separated memory and complete
network mediation are separate follow-ups; this pilot claims neither.

## Build and verify

Use a dedicated worktree. Build context contains exactly the four runtime
binaries, their hashes, this Dockerfile and the operator's shared instructions.
Never copy an environment file, home directory or credentials into that context.

```bash
python3 deploy/agent-isolation/pilot.py build \
  --context /tmp/buzz-isolation-build \
  --binary-dir /path/to/current/runtime/bin \
  --prompt /path/to/shared-base-prompt.md
PILOT_IMAGE=sha256:THE_PRINTED_IMAGE_ID \
  python3 -m unittest discover -s deploy/agent-isolation -v
```

The Docker journey uses the production launch arguments and real MCP shell.
It checks that an intentionally world-readable synthetic host secret is absent,
host processes/control sockets cannot be reached, image writes fail, and a
local repository can be created and committed. It never reads real sibling keys.

## Activate, check, roll back

Activation is host-side and requires access to the existing user service manager
and Docker. Check that the pilot is idle before stopping it. Keep the existing
service's environment file in place; the controller forwards an explicit list
of only that agent's settings, using variable **names**, not values, in argv.
Its HOME, PATH, commands and base-prompt paths come from the image. No credentials
are printed or written to a new file by this controller.

```bash
python3 deploy/agent-isolation/pilot.py activate \
  --image sha256:THE_PRINTED_IMAGE_ID \
  --env-file /path/to/buzz-acp-gemma.env
```

Activation stops and disables the old service before starting the replacement;
a failed replacement restores the original service. The replacement is enabled on boot.
The host service uses an immutable image ID; it does not follow image tags.
Inspect `systemctl --user status buzz-isolated-gemma.service` and the container
state. Verify relay reads from inside the container and exercise a normal chat
request before expanding to any other agent. A healthy service alone is not
proof of a successful model reply.

```bash
python3 deploy/agent-isolation/pilot.py rollback
```

Rollback stops and disables the pilot before restoring the original service,
and retains its named workspace. Do not run both versions of the same identity.
Audit shared credentials and old copies now; rotate affected credentials only
after removing the old shared access. Existing backups also need that treatment.

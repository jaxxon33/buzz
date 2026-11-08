# Agent isolation pilots

This operator deployment runs an existing `buzz-acp` + `buzz-agent` +
`buzz-dev-mcp` installation inside one non-root container. It changes no Buzz
wire protocol and does not rely on the launch-prefix hook being supported by
an older deployed binary. The harness, every worker, MCP server and shell run
inside the same boundary.

## Scope

The supported pilots are Gemma (UID/GID 61021) and Fizz (UID/GID 61022). Each has
its own service, container, private named workspace volume,
a read-only image and private PID/mount/network namespaces. No host home,
Docker socket, SSH agent, host D-Bus, privileged capabilities or host repository
is mounted. Fizz alone now has an explicitly provisioned read-only credential
volume containing only its own SSH key. Repository work starts with one local repo in the private volume;
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
are printed or written to a new file by this controller. The host resolves the
configured relay hostname at each launch. Because Tailscale binds its listener
to its own interface, the host forwards opaque TCP bytes from an ephemeral
docker0-only port to that one relay. An unprivileged loopback hop inside the
container connects to it. The original relay URL, TLS name and certificate
verification remain unchanged. No host network namespace is shared with Gemma.
The forwarding code limits concurrent connections to 16 and never decrypts TLS.

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


## Fizz

Use `--agent fizz` before the `build`, `activate`, `run` or `rollback` command.
This changes only the explicit Fizz service/container/volume names and worker UID;
Gemma remains the default for existing service files. Build a separate Fizz image
using the same runtime binaries and prompt. Run its tests with
`PILOT_AGENT=fizz PILOT_IMAGE=sha256:... python3 -m unittest discover -s deploy/agent-isolation -v`.

Fizz's existing HTTP model proxy is at 127.0.0.1:8100/v1 on the host. The host
controller forwards only that destination through another bounded docker0-only
TCP hop, and a loopback listener in the worker retains the existing provider URL.
This local HTTP traffic crosses only the host's Docker bridge. No arbitrary
forwarding endpoint, host networking or host MCP server is exposed by these tunnels.

The pilot uses the isolated buzz-dev-mcp shell/repo tools. Fizz's existing desktop
MCP merger is deliberately excluded because it can control machines outside the
boundary. The old service, custom MCP configuration and original nest are retained
for rollback; the pilot starts with a separate workspace and does not migrate old
worktrees, secret files, caches or symlink targets. It can create local test repos.
The same provider/model, reply guard, context/output limits and team instructions
are forwarded from Fizz's existing environment; no new account or billing is set up.


## Fizz SSH / GitHub workflow (phase 1)

Fizz's image includes openssh-client and an immutable SSH configuration for ag.
Build with `--ag-host <ag IP> --known-hosts <public pin file>`; the pin file must
contain exactly one `ag ssh-ed25519 ...` record derived from existing trusted
known_hosts, never a private key. The image/build context contains no private key.

The separate `buzz-isolated-fizz-ssh` credential volume contains only Fizz's
provisioned key, owned by UID/GID 61022, mode 0400 inside a mode-0500 directory.
It is mounted at /run/fizz-ssh with `readonly`. A trusted one-time helper copies
only the single Fizz key into that volume; Paul's SSH directory, shared key,
agent socket, SSH configuration and credential stores are never mounted.
SSH uses IdentitiesOnly, IdentityAgent none, strict pinned host verification,
BatchMode, no password/interactive fallback and no forwarding. Only ag is
configured; it uses Fizz's key at /run/fizz-ssh/id_ed25519.

This restores the existing explicitly accepted phase-1 authority: Fizz logs
into ag as paul and GitHub writes there use jaxxon33's existing credential.
That remains a powerful remote account; a separate key gives attribution and
revocation, not remote per-agent OS isolation. It is not a claim that ag's files,
keys or privileges are isolated from Fizz. The key is still readable by Fizz
and could be copied by a compromised worker; the read-only mount prevents edits,
not extraction. Remote account separation and the key broker remain later work.

Keep old image IDs for rollback. To rotate Fizz's key, reprovision only its
credential volume while the pilot is stopped and verify the public fingerprint
before restarting. Do not change shared SSH credentials or weaken host-key checks.

## Fizz desktop pilot

Extend the verified Fizz SSH image with `build_desktop.py`. Supply an immutable
local `--base` image ID, a digest-pinned `--node node@sha256:...` image,
fresh compiled broker `--compiled` output and a `--pin-file` containing one
trusted `hive-desk ssh-ed25519 ...` record. `--context` must not exist. The
builder copies only the desktop client's JavaScript dependency closure,
module metadata, fixed transport configuration and merge helper. Private keys,
broker state, host MCP configuration and owner/watch credentials never enter
the build context. The local base tag is derived from the supplied image ID
and verified after the build; its immutable identity is kept in the manifest.

The desktop image sets its own MCP command to `fizz-mcp`, combining the existing
contained buzz-dev-mcp with desktop-mcp. Node and both command wrappers are in
the read-only image. A dead child fails the merge so pending initialization or
tool discovery cannot silently hang. Message length and pending counts are
bounded; merge termination stops both children.

Provision only Fizz's existing desktop key into `buzz-isolated-fizz-desktop`,
owner 61022, file mode 0400 and directory mode 0500. Set restrictive modes
before transferring ownership when provisioning with dropped capabilities.
The immutable image label `buzz.isolation.desktop=fizz` makes the controller
mount that volume read-only at `/run/fizz-desktop`, in addition to the unchanged
workspace and separate ag key. Other image profiles receive no desktop key.

Desktop SSH uses a separate pinned host key, fixed `hive-desktop@10.0.64.160:22`
and the existing VM-side forced command `cu-exec --agent fizz`. The daemon
remains the authority for identity, lease, tools and Stop. Do not mount an
owner/watch key or replace the remote forced command with a general shell.
The existing bridge and model/relay/ag routes are retained. This does not add
an outbound firewall to the container: its previously documented open egress
remains; the VM's own desktop egress proxy/firewall remains authoritative.

Before cutover, run the full package with `PILOT_AGENT=fizz`, `PILOT_IMAGE` and
`PILOT_DESKTOP_IMAGE` set to the new immutable ID. Verify real merged shell and
desktop discovery, both SSH paths, public key fingerprint and owner-method
denial. Confirm the agent is idle, retain its workspace volume and previous
service/image for rollback, and exercise a disposable screenshot/inspect/click
task after restart. Finish by testing Hive's owner Stop and verifying further
agent input and lease acquisition are refused. Mac control is outside this pilot.

## Approved remaining buzz-agent batch

The controller also recognises the fifteen explicitly approved profiles from
bolt through venice in `PROFILES`, each with a distinct fixed UID and volume.
m5 and native adapters are excluded pending separate dependency review.
Max is approved separately after checking its existing model-server path from
inside a container; it has its own fixed UID and workspace, without SSH keys.
Only Fizz can receive SSH/desktop credential mounts. OpenAI-compatible and
OpenRouter provider settings are preserved independently, including sender
allowlists; target-undeclared forwarded variables are explicitly unset in the
unit so manager credentials cannot leak into a different identity.
The controller resolves the configured provider hostname on the host and adds
only that address to the container hosts table. Its URL and TLS hostname stay
unchanged; no host DNS configuration or network namespace is mounted. Addresses
are refreshed when the service starts.

Check actual workflow and idle state before each move. Activation validates the
selected image UID, stops the original and proves its PID is gone, then copies
only `/home/paul/buzz/nests/<selected-agent>` through a network-disabled,
temporary provisioning container. The source remains untouched. No host bind
mount is used. External symlinks and special files require a migration plan;
safe relative links are retained. An existing nonempty isolated volume is never
overwritten. File/link/directory content digests must match before the worker
starts. Failures restore the old service only after proving the replacement is
stopped. Validate the agent's actual new tools, chat and work before proceeding.

## Whole native-agent containers

The separate `native.py` controller adds the explicitly selected Claude, Codex,
Cursor and Omp agents. This supersedes the earlier native-agent exclusion only
for those profiles; `pilot.py` still controls the simpler buzz-agent family.
This is an operator rollout, not a new desktop deployment control plane.

`native_build.py` packages the installed adapter and native runtime without
upgrading either. It records code hashes and immutable base/image identities.
`reprofile()` reuses a verified runtime image and changes only its worker UID.
Build contexts contain runtime code and shared instructions, never auth state.

`native_prepare.py` copies the selected agent's workspace, its own runtime
home/project memory, and its separate SSH key into private named volumes. The
worker mounts the key read-only, with strict host verification and no shared
key fallback. Claude-Opus also retains its own desktop key in a separate
read-only volume. No host home, source repository, control socket or host
namespace is mounted. The workspace volume is mounted at both `/workspace`
and the original nest path so saved project and worktree paths remain valid.

This preserves the existing approved remote-account authority: SSH access as
paul on other machines remains powerful. Separate worker keys provide
attribution and revocation; they do not isolate that remote account or prevent
a compromised worker from reading its own key. Provider and Buzz credentials
also remain available to their selected runtime. Egress is not fully mediated.

Before activation:

1. Confirm every channel session of the selected identity is idle. Do not
   interrupt a tool job merely because its process looks like a watcher.
2. Prepare private copies and an agent/image/PID/settings/workspace snapshot.
   A previous failed preparation is not a usable snapshot. Inspect retained
   partial volumes and preserve work before deliberately reconciling them.
3. Resolve external Git worktree metadata into private copies. Copy object
   data without shared alternates/hardlinks, retain the exact HEAD/index and
   working files, and verify Git status and object connectivity inside the
   worker. Never mount the common host repository.
4. Run `native_check.py` with the unchanged model and a fresh probe label.
   It drives the real adapter and native shell, independently verifies written
   bytes and the Git index, and checks an authenticated relay read. Use
   `--ssh-host ag`, `m2` or `m4` to verify the selected existing remote login.
   The preflight does not start a second relay-listening identity.
5. Refresh and verify project memory after in-flight work finishes. Verify
   additional configured tools separately; listing desktop tools proves
   discovery, not remote desktop interaction.

Activate with the immutable image and snapshot:

```bash
python3 deploy/agent-isolation/native.py --agent NAME activate \
  --image sha256:IMAGE_ID --env-file /path/to/existing-agent.env \
  --snapshot /path/to/verified-snapshot.json
```

Activation refuses a changed original PID, settings file or workspace. It
stops/disables the original before starting the replacement, rechecks the
workspace after the stop, and restores the original on failure only after
proving that the replacement is gone. Inspect the actual launch with
`native.audit()` and exercise a normal relay-driven task after switching.
A healthy process or a direct adapter preflight alone is not that final check.

Rollback uses `native.py --agent NAME rollback`. Recover any new work from the
private volumes before resuming editing in the original host workspace.
Never run both copies of one identity. Moving the active operator's own agent
requires an independent controller and a verified continuation path.

Full package verification uses the existing `unittest discover` command with
`PILOT_IMAGE` and `NATIVE_IMAGE` set to the exact images being checked. Set
`PILOT_AGENT`/`NATIVE_AGENT` to their matching profiles. Native negative controls
use disposable empty volumes, not production credentials. Workspace validation
and hashing must fail on unreadable directories instead of silently omitting
saved work. Timed-out copy helpers are removed by exact disposable container
name; source files and copied volumes are retained.

Max separately requires an image labelled `buzz.isolation.max_app=1` containing
its existing Node server. `max_contained.py` supervises that server alongside
the harness, and the controller retains its existing published port 8080. Its
unchanged model must work on the host and in the prepared container before a
service switch; an advertised model listing is not a generation check.
Max's private workspace volume also appears at its original nest path so saved
absolute Git worktree links keep working. This mounts the same private volume
at two paths; it does not mount the original host workspace or host home.

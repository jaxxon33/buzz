# Isolated whole-agent container — native runtimes (prototype)

Status: **prototype**. This puts the *whole* agent in one container for Buzz
agents that use a native coding runtime:

- Claude Code via the ACP adapter **`@agentclientprotocol/claude-agent-acp`**
  (binary `claude-agent-acp`). This is the current name; the older
  `@zed-industries/claude-code-acp` and `@zed-industries/claude-agent-acp`
  packages are deprecated on npm and point here. `buzz-acp` still accepts the
  legacy `claude-code-acp` command name.
- OpenAI Codex via **`@agentclientprotocol/codex-acp`** (binary `codex-acp`;
  it replaces the deprecated `@zed-industries/codex-acp` and pulls in
  `@openai/codex`).

Versions are pinned in `Dockerfile.native`: **`claude-agent-acp@0.94.0`** and
**`codex-acp@2.2.2`** (`codex-acp` pulls `@openai/codex`; the image resolved `codex-cli 0.160.1`). Those are
**not** the latest releases. On 2026-11-03 npm had 0.100.0 and 2.5.0.

> **Compatibility finding: the latest adapters cannot be started by this
> `buzz-acp`.** From `claude-agent-acp` 0.95.0 and `codex-acp` 2.3.0, both
> adapters implement upstream ACP v2 (`@agentclientprotocol/sdk` 2.x). There,
> `initialize` requires an `info` object. `buzz-acp` in this tree
> (`build_initialize_params` in `crates/buzz-acp/src/acp.rs`) deliberately
> sends a pre-RFD "v2" initialize (`protocolVersion: 2` + `clientInfo`). The
> new adapters reject it with `-32602 Invalid params (path: info)`, and the
> harness exits with `all 1 agents failed to start`. 0.94.0 and 2.2.2 are the
> newest versions that accept it (they negotiate down to `protocolVersion: 1`).
> This is a harness bug, outside this directory's scope. Until it is fixed,
> don't bump the pins without re-running `probe.sh`. Its `buzz-acp`-mode
> initialize check fails on the incompatible versions.

```
┌──────────────────── container (uid 61000, no host home, no docker.sock) ────┐
│ tini(--init) ─► entry ─► buzz-acp ──stdio/ACP──► claude-agent-acp | codex-acp  │
│                          │                         └─ built-in shell/tools  │
│                          └─ WS to relay            (git, ssh, buzz CLI, rg) │
│ /workspace          ← per-agent volume (rw)                                 │
│ ~/.claude ~/.codex  ← per-agent state volumes (rw: login + token refresh)   │
│ /run/agent/env      ← env file (ro bind)                                    │
│ /run/agent/ssh/     ← the agent's own ssh key + known_hosts (ro bind)       │
└─────────────────────────────────────────────────────────────────────────────┘
```

Why the whole runtime goes in: the existing pilot isolates `buzz-acp` +
`buzz-agent` + its MCP tool server, where every tool call goes through the MCP
server we control. Native runtimes (`claude-agent-acp`, `codex-acp`) ignore
that and run tools through their **own** built-in shell. Their shell has the
same view of the filesystem as the adapter, so the only way to bound it is to
put the harness, the adapter, and the shell together in the container.

## Files

| File | Purpose |
|------|---------|
| `Dockerfile.native` | Builds `buzz-acp` + `buzz` (CLI) from `crates/`, then a Node 22 runtime with both ACP adapters, git, openssh-client (plus curl, jq, ripgrep when apt is used). Runs as uid/gid 61000. Workspace `/workspace`. Run it with `docker run --init`, which makes tini PID 1. |
| `entrypoint.sh` | Loads the read-only env file (parsed as `KEY=VALUE`, never `source`d), wires `GIT_SSH_COMMAND` to the mounted key, `exec buzz-acp`. |
| `probe.sh` | In-container isolation + runtime probe (installed in the image as `buzz-isolation-probe`). |

## Build

From the repo root:

```bash
docker build -f deploy/isolated-agent/Dockerfile.native -t buzz-native-agent:dev .
```

Optional build inputs (only needed in restricted networks):

- `--build-arg BASE_REGISTRY=mirror.gcr.io/library` — base images from a
  mirror when Docker Hub rate-limits (`429 Too Many Requests`).
- `--secret id=extra_ca,src=/path/proxy-ca.pem` — a TLS-intercepting proxy's
  CA, used **only during the build** (cargo, apt, npm). It is passed as a
  BuildKit secret and the runtime stage regenerates its CA bundle from the
  distro package, so the proxy CA is not shipped in the image.
- `--network=host --build-arg HTTPS_PROXY=... --build-arg NO_PROXY=...` when the
  proxy listens on the host's loopback.
- `--build-arg RUNTIME_VARIANT= --build-arg INSTALL_APT_PACKAGES=0` builds the
  runtime from the full `node:22-bookworm` image instead of `-slim` + apt. The
  full image already contains git, openssh-client, curl, and ca-certificates.
  Use it where the Debian mirrors are unreachable. This variant is bigger
  (2.8 GB here; the slim variant could not be built in the verification VM, see below) and has no `jq` or `ripgrep`. The
  build fails if git or ssh is missing in either variant.

## Required mounts per agent

Each agent gets **its own** set. Nothing is shared between agents.

| Host source | Container path | Mode | Why |
|---|---|---|---|
| `/etc/buzz-agents/<name>.env` (owner `61000`, mode `0400`) | `/run/agent/env` | **ro** bind | `BUZZ_PRIVATE_KEY`, `BUZZ_RELAY_URL`, `BUZZ_ACP_AGENT_COMMAND`, `ANTHROPIC_API_KEY`/`CLAUDE_CODE_OAUTH_TOKEN` or `OPENAI_API_KEY`, `BUZZ_ACP_RESPOND_TO`, … Read by the entrypoint, so the secrets do **not** show up in `docker inspect` the way `--env-file` values do. |
| volume `buzz-<name>-claude` | `/home/agent/.claude` | rw volume | Claude Code login + refresh state. `CLAUDE_CONFIG_DIR` points here, so `.claude.json` lives here too, not in `$HOME`. |
| volume `buzz-<name>-codex` | `/home/agent/.codex` | rw volume | Codex `auth.json`, config, sessions (`CODEX_HOME`). |
| volume or dir `buzz-<name>-workspace` | `/workspace` | rw | The agent's working tree. It is the only place where repo state persists. |
| `/etc/buzz-agents/<name>.ssh/` holding `id_ed25519` (owner `61000`, `0400`) and `known_hosts` | `/run/agent/ssh` | **ro** bind | The agent's **own** deploy key. It is never the operator's key. The entrypoint sets `GIT_SSH_COMMAND="ssh -i … -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes -o UserKnownHostsFile=…"`. |

**Explicitly not mounted:** nothing from the host home: no `~/.ssh`, no
`~/.claude`, no `~/.codex`, no `~/.gitconfig`, no `~/.config`, no `$HOME`
subdirectory of any kind. There is **no `/var/run/docker.sock`** and no
`DOCKER_HOST`, the container is not `--privileged`, and it gets no host
network namespace (it uses the default bridge or a dedicated network).

`ssh` refuses a key that other users can read, and a read-only bind mount
keeps the host's ownership. So install the key as `install -o 61000 -g 61000
-m 0400 …` on the host. A key owned by the operator uid cannot be read by uid
61000.

### Login and token refresh

Both runtimes keep OAuth state in files and **rewrite those files when they
refresh tokens**:

- Claude Code: `$CLAUDE_CONFIG_DIR/.credentials.json` (+ `.claude.json`).
- Codex: `$CODEX_HOME/auth.json` (ChatGPT login refreshes `access_token`/
  `refresh_token` and rewrites the file).

Because those directories are per-agent **writable volumes**, a refresh inside
the container writes the new tokens into the volume. They survive restarts and
image upgrades, and the host home is never involved. If the volume were
read-only, the runtime would refresh in memory, fail to persist, and break
after the next restart once the old refresh token had been rotated away.

To seed a volume, use one of these (per agent):

1. **Static credential, no refresh (recommended for 29 headless agents):** put
   `ANTHROPIC_API_KEY` or `CLAUDE_CODE_OAUTH_TOKEN` (from `claude setup-token`
   on any machine; it is long-lived) or `OPENAI_API_KEY` in the env file. The
   state volumes then only hold config/session history.
2. **Interactive login inside a one-off container that uses the same volume.**
   For Codex the image ships the `codex` CLI from codex-acp's dependency:

   ```bash
   docker run --rm -it --entrypoint codex \
     -v buzz-<name>-codex:/home/agent/.codex buzz-native-agent:dev login --device-auth
   ```

   For Claude, run the Claude Code CLI the same way against the
   `buzz-<name>-claude` volume
   (`--entrypoint npx … @anthropic-ai/claude-code /login`). It is not
   preinstalled.

**Do not copy one login into several agents' volumes.** OAuth refresh tokens
rotate. The first agent to refresh invalidates the copy held by every other
agent, and those agents then fail auth. Use one login (or one static key) per
agent.

## Launching from systemd

A template unit runs one container per agent: `buzz-agent@<name>.service`.

```ini
# /etc/systemd/system/buzz-agent@.service
[Unit]
Description=Buzz native agent %i (isolated container)
After=docker.service network-online.target
Requires=docker.service

[Service]
Restart=on-failure
RestartSec=10
# A clean exit (e.g. operator stop) must not be revived; see docs/remote-agents.md (I3).
RestartPreventExitStatus=0
# docker run exits 143 when the container is stopped with SIGTERM.
SuccessExitStatus=143
ExecStartPre=-/usr/bin/docker rm -f buzz-agent-%i
ExecStart=/usr/bin/docker run --rm --name buzz-agent-%i --init \
  --user 61000:61000 \
  --read-only --tmpfs /tmp:rw,nosuid,nodev,size=512m \
  --tmpfs /home/agent:rw,nosuid,nodev,uid=61000,gid=61000,mode=0700,size=64m \
  --cap-drop ALL --security-opt no-new-privileges \
  --pids-limit 1024 --memory 4g --cpus 2 \
  --network buzz-agents \
  -v /etc/buzz-agents/%i.env:/run/agent/env:ro \
  -v /etc/buzz-agents/%i.ssh:/run/agent/ssh:ro \
  -v buzz-%i-claude:/home/agent/.claude \
  -v buzz-%i-codex:/home/agent/.codex \
  -v buzz-%i-workspace:/workspace \
  buzz-native-agent:dev
ExecStop=/usr/bin/docker stop -t 30 buzz-agent-%i

[Install]
WantedBy=multi-user.target
```

```bash
docker network create buzz-agents      # once
systemctl enable --now buzz-agent@alice
```

Notes:

- `--read-only` + tmpfs `$HOME`: the root filesystem is immutable. `$HOME` is
  scratch space for `~/.gitconfig`, `~/.ssh`, and npm caches. The three volumes
  mount on top of the tmpfs, so login state and the workspace persist and
  everything else is discarded on restart.
- `--init` makes Docker's tini PID 1. `docker stop` sends SIGTERM to tini,
  which forwards it to `buzz-acp`; tini also reaps the zombies that the
  runtimes' shell commands leave behind.
- The systemd unit runs as root to talk to dockerd. That is the host's trust
  boundary. **The agent itself never sees the socket.** Rootless Docker or
  Podman (`podman run --userns=keep-id …`, Quadlet) is a drop-in alternative
  that also removes root dockerd from the picture.

## Risks that remain

- **Secrets are still inside the container.** The agent's Nostr key
  (`BUZZ_PRIVATE_KEY`), model credentials, OAuth refresh tokens, and the ssh
  deploy key are all readable by uid 61000. The native runtime's shell runs
  as that uid. A prompt-injected agent can read them and exfiltrate them. The
  container limits the blast radius to **this agent's** credentials. It does
  not protect them. Mitigations: scoped deploy keys (one repo, read-only where
  possible), per-agent API keys with spend limits, and key rotation.
- **Network egress is open by default.** The runtime needs the model API, the
  relay, and git remotes. Anything else it can also reach, including the
  cloud metadata endpoint (`169.254.169.254`), other containers on the bridge,
  and LAN hosts. Put agents on a dedicated network with an egress allowlist
  (an HTTP CONNECT proxy, or nftables on the `buzz-agents` bridge allowing
  only the relay, `api.anthropic.com`, `api.openai.com`/`chatgpt.com`, and git
  hosts), and block the metadata IP.
- **Shared kernel.** It is still one VM and one kernel. A container escape
  reaches root dockerd. Rootless Docker/Podman or gVisor (`--runtime=runsc`)
  reduce this.
- **The workspace is writable** by design: the agent can destroy its own
  work. Back up the volumes, or push work to remotes.
- **Image supply chain:** npm adapters and `@openai/codex` are third-party
  code running with the agent's credentials. Versions are pinned. Review
  upgrades.
- **Resource exhaustion:** set `--memory`, `--cpus`, and `--pids-limit` as in
  the unit above, otherwise one runaway agent starves the other 28.

## Verification

Run on 2026-11-03 in an ephemeral Linux cloud VM (kernel 6.18, x86_64,
Docker Engine 29.9.2, cgroup v1). The VM is **not** the owner's agent VM.
Docker's CLI was installed but the daemon wasn't running, so it was started by
hand (`dockerd &`). Results below are copied from the real runs.

### Environment blockers hit (and what was done)

| Blocker | Effect | Workaround used |
|---|---|---|
| Docker Hub `429 Too Many Requests` on `debian`/`rust`/`node` pulls | base images unpullable from docker.io | `--build-arg BASE_REGISTRY=mirror.gcr.io/library` |
| TLS-intercepting egress proxy on `127.0.0.1:46417` | cargo git deps and npm need the proxy CA | `--network=host`, proxy build args, `--secret id=extra_ca,src=<proxy CA>` (build-only, not shipped) |
| Egress policy returns **403 for `deb.debian.org`** (HTTP and HTTPS) | the default `-slim` + `apt-get` runtime stage **failed** (`E: Package 'ca-certificates' has no installation candidate`, after `Invalid response from proxy: HTTP/1.1 403 Forbidden`) | not worked around (policy denial). Verified with `RUNTIME_VARIANT= INSTALL_APT_PACKAGES=0` (full `node:22-bookworm`). **The default slim/apt build path is untested** and should be built once on the owner's VM. |
| No relay in the VM | `buzz-acp` can't complete a real turn | verified up to `agent_pool_ready` + relay connect attempts; **no live mention→reply turn and no model login were exercised** |

The Rust builder stage itself built cleanly: `cargo build --locked --release
-p buzz-acp -p buzz-cli` finished in 2m36s.

### Build (verified variant)

```bash
docker build --network=host \
  --build-arg BASE_REGISTRY=mirror.gcr.io/library \
  --build-arg RUNTIME_VARIANT= --build-arg INSTALL_APT_PACKAGES=0 \
  --build-arg HTTPS_PROXY="$HTTPS_PROXY" --build-arg https_proxy="$HTTPS_PROXY" \
  --build-arg NO_PROXY="$NO_PROXY" --build-arg no_proxy="$NO_PROXY" \
  --secret id=extra_ca,src=/root/.ccr/ca-bundle.crt \
  -f deploy/isolated-agent/Dockerfile.native -t buzz-native-agent:dev .
# → exit 0, image 2.8GB
```

### Probe: hardened run, pinned adapters: **all passed**

The canary is planted on the host first:
`mkdir -p /home/user/.buzz-canary-dir && echo c > /home/user/.buzz-canary-dir/secret`.
Plant it somewhere other than `/home/agent` or `/workspace`, which exist
inside the container by design.

```bash
docker run --rm --init --user 61000:61000 \
  --read-only --tmpfs /tmp:rw,nosuid,nodev \
  --tmpfs /home/agent:rw,nosuid,nodev,uid=61000,gid=61000,mode=0700 \
  --cap-drop ALL --security-opt no-new-privileges \
  -v probe-claude:/home/agent/.claude -v probe-codex:/home/agent/.codex -v probe-ws:/workspace \
  -e PROBE_HOST_CANARY=/home/user/.buzz-canary-dir/secret -e PROBE_EXPECT_UID=61000 \
  --entrypoint /usr/local/bin/buzz-isolation-probe buzz-native-agent:dev
# → exit 0
```

```
PASS  non-root: uid=61000(agent) gid=61000(agent) groups=61000(agent)
PASS  uid matches expected 61000
PASS  no docker socket (/var/run/docker.sock, /run/docker.sock absent)
PASS  DOCKER_HOST unset
PASS  planted host path not visible (and no ancestor is a mount): /home/user/.buzz-canary-dir/secret
PASS  no mount sourced from /home/* or /root
INFO  non-system mounts:
        /usr/sbin/docker-init (/usr/libexec/docker/docker-init) ro,relatime
        /tmp (/) rw,nosuid,nodev,noexec,relatime
        /workspace (/var/lib/docker/volumes/probe-ws/_data) rw,relatime
        /etc/hostname (/var/lib/docker/containers/<id>/hostname) ro,relatime
        /etc/hosts (/var/lib/docker/containers/<id>/hosts) ro,relatime
        /etc/resolv.conf (/var/lib/docker/containers/<id>/resolv.conf) ro,relatime
        /home/agent (/) rw,nosuid,nodev,noexec,relatime
        /home/agent/.claude (/var/lib/docker/volumes/probe-claude/_data) rw,relatime
        /home/agent/.codex (/var/lib/docker/volumes/probe-codex/_data) rw,relatime
PASS  git present: /usr/bin/git (git version 2.39.5)
PASS  ssh present: /usr/bin/ssh (OpenSSH_9.2p1 Debian-2+deb12u10, OpenSSL 3.0.22 25 Aug 2026)
PASS  buzz-acp present: /usr/local/bin/buzz-acp (--help ok)
PASS  buzz present: /usr/local/bin/buzz (--help ok)
PASS  node present: /usr/local/bin/node (v22.23.4)
PASS  ACP initialize (v1): claude-agent-acp -> protocolVersion=1 agent=@agentclientprotocol/claude-agent-acp@0.94.0 authMethods=none
PASS  ACP initialize (buzz-acp): claude-agent-acp -> protocolVersion=1 agent=@agentclientprotocol/claude-agent-acp@0.94.0 authMethods=claude-ai-login,console-login
PASS  ACP initialize (v1): codex-acp -> protocolVersion=1 agent=@agentclientprotocol/codex-acp@2.2.2 authMethods=api-key,chat-gpt
PASS  ACP initialize (buzz-acp): codex-acp -> protocolVersion=1 agent=@agentclientprotocol/codex-acp@2.2.2 authMethods=api-key,chat-gpt

probe: all checks passed
```

### Probe: latest adapters (0.100.0 / 2.5.0): **failed (compatibility finding)**

Same probe against an image built with the then-latest adapter versions:

```
PASS  ACP initialize (v1): claude-agent-acp -> protocolVersion=1 agent=@agentclientprotocol/claude-agent-acp@0.100.0 authMethods=none
FAIL  ACP initialize (buzz-acp): claude-agent-acp -> error response: {"code":-32602,"message":"Invalid params","data":"invalid initialize params: [{expected: object, code: invalid_type, path: [info], message: Invalid input: expected object, received undefined}]"}
PASS  ACP initialize (v1): codex-acp -> protocolVersion=1 agent=@agentclientprotocol/codex-acp@2.5.0 authMethods=api-key,chat-gpt
FAIL  ACP initialize (buzz-acp): codex-acp -> error response: {"code":-32602,"message":"Invalid params","data":"invalid initialize params: [{expected: object, code: invalid_type, path: [info], message: Invalid input: expected object, received undefined}]"}
probe: 3 check(s) failed
```

The same image run as an agent (`buzz-acp` via the entrypoint) logged:
`ERROR buzz_acp: agent initialize failed: Agent reported error (code -32602): Invalid params`
→ `Error: all 1 agents failed to start — cannot continue`.

I bisected with the exact `buzz-acp` initialize params (stable releases
only):

| adapter | accepts buzz-acp initialize | rejects (`-32602`, needs `info`) |
|---|---|---|
| `@agentclientprotocol/claude-agent-acp` | 0.80.0, 0.90.0–0.94.0 | 0.95.0, 0.97.0, 0.98.0, 0.99.0, 0.99.1, 0.100.0 |
| `@agentclientprotocol/codex-acp` | 1.13.1, 2.0.0, 2.2.2 | 2.3.0, 2.4.0, 2.5.0 |

### Negative controls: the probe catches violations

```bash
# A: docker socket + a host-home dir bind-mounted
docker run --rm --init --user 61000:61000 -v /var/run/docker.sock:/var/run/docker.sock \
  -v /home/user/.buzz-canary-dir:/home/user/.buzz-canary-dir:ro \
  -e PROBE_HOST_CANARY=/home/user/.buzz-canary-dir/secret -e PROBE_ACP_ADAPTERS= \
  --entrypoint buzz-isolation-probe buzz-native-agent:dev
FAIL  docker socket present at /var/run/docker.sock
FAIL  docker socket present at /run/docker.sock
FAIL  planted host path is visible inside the container: /home/user/.buzz-canary-dir/secret
FAIL  mounts sourced from a host home directory:
probe: 4 check(s) failed

# B: host /root (mode 0700, so uid 61000 cannot even stat the canary) mounted
docker run ... -v /root:/root:ro -e PROBE_HOST_CANARY=/root/.buzz-isolation-canary ...
FAIL  planted host path's ancestor /root is a mount point
FAIL  mounts sourced from a host home directory:
probe: 2 check(s) failed

# C: run as root
docker run --rm --user 0 ... --entrypoint buzz-isolation-probe buzz-native-agent:dev
FAIL  running as root (id -u = 0)
probe: 1 check(s) failed
```

The first version of the canary check missed case B: a plain `-e` test can't
see through a 0700 directory. It was fixed by also failing when any ancestor of
the canary is a mount point.

### End-to-end harness start (no relay)

Env file and ssh dir were prepared like production (owner 61000, `0400`/`0500`,
mounted `:ro`). `BUZZ_RELAY_URL=ws://127.0.0.1:9` (nothing listening),
`BUZZ_PRIVATE_KEY` = random test key, no model credentials.

```bash
docker run --rm --init --user 61000:61000 --read-only --tmpfs /tmp \
  --tmpfs /home/agent:uid=61000,gid=61000,mode=0700 --cap-drop ALL --security-opt no-new-privileges \
  -v $PWD/env:/run/agent/env:ro -v $PWD/ssh:/run/agent/ssh:ro \
  -v probe-claude:/home/agent/.claude -v probe-codex:/home/agent/.codex -v probe-ws:/workspace \
  buzz-native-agent:dev
```

Both `BUZZ_ACP_AGENT_COMMAND=codex-acp` and `=claude-agent-acp` reached:

```
INFO buzz_acp: agent initialized agent=0 name="@agentclientprotocol/codex-acp" steering_supported=true
INFO buzz_acp: agent_pool_ready agents=1
WARN buzz_acp::relay: initial relay connect attempt 0 failed: WebSocket error: IO error: Connection refused (os error 111)
...
INFO buzz_acp: agent initialized agent=0 name="@agentclientprotocol/claude-agent-acp" steering_supported=true
INFO buzz_acp: agent_pool_ready agents=1
```

Other checks in the same setup:

- Entrypoint env parsing → `BUZZ_RELAY_URL`, `BUZZ_ACP_AGENT_COMMAND` exported.
  `GIT_SSH_COMMAND=ssh -i /run/agent/ssh/id_ed25519 -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes -o UserKnownHostsFile=/run/agent/ssh/known_hosts`.
- `ssh-keygen -y -f /run/agent/ssh/id_ed25519` as uid 61000 → key readable.
  `touch /run/agent/ssh/x` → `Read-only file system`.
- `docker top` while running: `docker-init`, `buzz-acp`, and
  `node /usr/local/bin/claude-agent-acp`, all as uid 61000.
- `docker stop -t 30` → container exited in 0.2s with 143 (SIGTERM handled; no
  30s kill timeout).
- `codex --version` in the pinned image → `codex-cli 0.160.1` (the `codex`
  CLI that codex-acp 2.2.2 depends on, exposed for `codex login`).

### Not verified

- The default `-slim` + apt build (blocked by egress policy here).
- A real relay turn (mention → adapter → shell → `buzz` reply), model login,
  and OAuth token refresh persisting in the volumes. The refresh behaviour
  described above comes from the runtimes' documented file locations
  (`$CLAUDE_CONFIG_DIR/.credentials.json`, `$CODEX_HOME/auth.json`). It was not
  observed.
- The systemd unit (no systemd in the verification VM). It was written to
  match the `docker run` flags that were verified.
- An actual `git push` over ssh with the mounted key (no reachable remote).

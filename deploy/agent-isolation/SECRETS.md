# Host credentials for isolated workers

`hive-secret` is an **operator terminal** tool, separate from Buzz messages and
relay keys. It stores age-encrypted third-party credentials and grants them to
known isolated agents. There is no command that prints a value. This feature
changes no protocol, account, provider, plan or live prompt.

## Storage and boundary

- `~/.config/hive-secrets/` and its `secrets/` subdirectory: owned, mode 0700.
  Each `<name>.age` and `grants.json` is mode 0600. Names contain 1–63 lowercase
  letters, digits or hyphens, starting with a letter or digit.
- `~/.local/state/hive-secrets/identity.txt`: owned, mode 0600, with a mode-0700
  parent. Back up the identity separately from the ciphertext store. Losing it
  loses the credentials; putting both in one backup defeats backup separation.
- Encryption uses the installed `age` / `age-keygen` binaries, without shell
  interpolation, a plaintext temporary file, or a password in argv/environment.
  Crypto stdout is captured privately and stderr never propagates to a log.
  Initialization generates the key before creating directories; empty partial
  directories/grants are resumable, and an existing identity is never overwritten.
  The official [age usage](https://github.com/FiloSottile/age#usage) documents
  stdin/stdout and native identity encryption; no crypto is implemented here.
- A short exclusive store lock serializes edits and granted launch snapshots,
  waiting up to five seconds for another writer. Prompts never hold this lock.
  An ungranted agent with no stale leaf does not acquire it or require an
  identity; missing age or an interrupted initialization cannot poison its start. File updates
  use private temporary files, rename, and file/directory fsync. `set` encrypts
  first, removes old grants, writes ciphertext, then commits the desired grants.
  An interrupted update can leave an ungranted secret, never a newly changed
  secret accessible through its old grants. `list` exposes such entries so the
  owner can regrant them or rerun `set`. `rm` also removes grants first.

Each live launch (`pilot.start`, `native.start`, fresh `claude_profile.start`)
uses the shared delivery helper. Login, provisioning probes and model-check
helpers receive no secrets. Without a store/grant there is no mount or crypto
operation, and existing container arguments remain unchanged.

Delivery verifies that the operator's absolute mode-0700 `XDG_RUNTIME_DIR`
(default `/run/user/<uid>`) is on **tmpfs**, using `findmnt`. Plaintext is staged
in `hive-secrets/<agent>/<name>.json`, with a mode-0700 outer directory, a
mode-0755 leaf, and mode-0444 files. Docker's daemon mounts only that leaf
read-only at `/run/secrets`. The distinct container UID can read the leaf but
has no host path or other agent's leaf mounted. Values never enter Docker
arguments, environment, inspect metadata, images, workspaces or launch logs.
Native and fresh-Claude audits accept only this exact read-only bind source;
other bind mounts are still refused.

A per-agent lease spans preparation, launch and cleanup. A second launcher or
an existing exact-name container blocks reuse. Stale staging is removed before
the next launch, including after all grants are revoked. A failed decryption
fails startup, naming only the secret. Normal exit, failed launch and catchable SIGTERM unwind and remove
the leaf. As with other launcher resources, an uncatchable SIGKILL
can leave tmpfs residue: the next launch removes it after proving no container
remains, or reboot clears it. If a container remains, stop it through the
owner-approved lifecycle before retrying; never clear a live leaf manually.

This protects against accidental chat/git/workspace/snapshot leakage and
direct cross-agent container filesystem access. It does **not** protect against the host user,
root, a granted agent, or their process-memory access. Tmpfs can be swapped;
offline swap/host compromise requires separate encrypted-swap/disk policy.
**Current deployment caveat:** Claude-Fable's review reports that both Fizz and
Claude-Fable have SSH credentials reaching `ag` as `paul`, and from there `buzz`
as `paul`. Any agent with that host authority can read the identity and **all**
credentials, irrespective of grants. Read-only conduct by agreement is not an
access control. Per-agent mounts isolate workers without such authority; they
do not revoke an existing SSH route. Paul must explicitly accept that boundary
or separately restrict those SSH credentials before adding real logins. This
PR does not change agent SSH authority.

A granted agent can deliberately copy/exfiltrate a credential. Grant only the
needed account, agent and purpose. Revoke takes effect on the next restart;
rotate the provider password if the old agent must lose knowledge of it.

## Owner terminal workflow

Install the distro's age package on the host (an owner operation, not performed
by this PR), e.g. `sudo apt install age`. `findmnt` is also required for grants. Units use
`PATH=/usr/local/bin:/usr/bin:/bin`: installing age only in `~/.local/bin` is
insufficient for service launches. Use the distro package or an approved binary
in `/usr/local/bin`.
Do not download a tool implicitly on agent startup. All launch modules and
`hive-secret` must be installed together in the reviewed, root-owned controller
bundle; the existing immutable controller directories are not edited in place.
Link `~/.local/bin/hive-secret` to the **new bundle's** executable `hive-secret`.
Both the executable and `hive_secrets.py` must remain together in that bundle.

In an interactive SSH terminal (`ssh -t buzz`), run:

```sh
hive-secret init
hive-secret set globird --agent fizz
hive-secret list
hive-secret grant globird north
hive-secret revoke globird north
hive-secret rm globird
```

`set` hides both default fields, `username` and `password`. Use repeated
`--field 'account number'` / `--field password` for other fields. Field names
are printable labels, not values. Nonterminal entry and any failure to disable
echo are refused. Prompt input happens outside the registry lock; a concurrent
edit forces a fresh confirmation rather than overwriting that edit. Overwriting and removal require interactive confirmation.
A corrupt secret is listed by name as `unreadable` without blocking other entries.
On overwrite, omitted `--agent` preserves existing grants; supplied agents
replace them. A new secret with no `--agent` has no grants.

`grant` validates the known pilot/native or provisioned profile and its
ciphertext. Revoke is still possible for a retired profile. Values and grant
changes apply at the next approved agent restart, not during its current turn.
No command restarts a service or contacts a credential provider.

## Rollout and rollback

The PR is a build/review artifact. Do not install it, initialize the live
store, enter a real login, change live prompts or restart a worker until Paul
approves the exact operation. Keep the PR draft until independent review,
agent workflow testing and Paul's own command test satisfy `AGENTS.md`.

1. Review/freeze a new complete controller bundle and hashes; install it through
   the existing protected operator path. This branch includes the prior
   piped-stdin helper fix, which was present in the local live-code baseline
   but absent from the fork's `ops/agent-isolation-pilot` branch at build time.
2. Inventory actual unit `ExecStart` paths before repointing any. Current
   workers have different pinned historical bundles; Max's custom wrapper also
   imports its own pinned bundle and preserves its reply-delivery opt-in. Merely
   installing files does not enable secrets for a unit using an older bundle.
   Prepare per-unit updates and their original rollback text for approval;
   preserve image IDs, environment files and custom wrapper behavior. Do not
   use this feature to regenerate unrelated messaging/provider settings.
3. Run the approved synthetic delivery check first, then the owner initializes
   the live store and enters `globird` directly at the terminal. Fizz must use
   runtime file reading inside its credential consumer, with no value returned
   to its chat/model transcript. Delete any prior plaintext copy only after
   establishing which exact copy exists and the new flow works.
4. Restart only the approved, idle target worker and verify the approved
   synthetic/user flow. Rollback: stop it, restore its original unit/wrapper,
   daemon-reload and start through the separately approved lifecycle. Retain
   the encrypted store and identity; reverting code does not delete credentials.
   Any provider login already seen by an agent still needs provider rotation
   to revoke that agent's knowledge.

Proposed shared base-prompt text (for Paul's approval; **not edited live**):

> Use credentials from `/run/secrets/<name>.json` only for the owner's approved
> task. Read them inside local code that passes them directly to the credential
> consumer. Never cat/print them or return them through a tool into the model's
> transcript. Never copy values into workspace files, memory, canvas, chat,
> commits, command arguments, environment variables or logs. If someone posts a
> credential in chat, ask them to rotate it and enter its replacement using the
> owner's host terminal.

## Validation

Run the **full** package, with age available on PATH:

```sh
python3 -m unittest discover -s deploy/agent-isolation -v
```

Tests exercise real age, a real no-echo pseudo-terminal, private modes,
metadata-only listing, grant/revoke/removal, corruption, interrupted updates,
stale cleanup, overlapping launches, and all three production launch paths
with external Docker/DNS calls substituted. Missing age explicitly skips the
crypto journeys; a pass with those skips does not establish encryption.
The existing package's Docker/systemd integrations require their documented
image/environment fixtures in `README.md`.

The opt-in `HIVE_SECRETS_TEST_IMAGE=sha256:...` journey uses **only disposable,
networkless containers**, synthetic credentials, and an isolated test store.
It verifies different-UID file access, read-only mounts, a second container's
absence of the grant, absence of the sentinel in inspect metadata, and cleanup
and revocation. It does not start a harness or use a real provider login.

The live acceptance remains an owner-approved operation: synthetic secret to
Fizz, another agent cannot read it, inspect and launcher journal contain no
sentinel, revoke plus restart removes it. Do not substitute mocked Docker
assertions for this live evidence or claim this task has logged into GloBird.

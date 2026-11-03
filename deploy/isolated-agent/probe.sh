#!/bin/bash
# Isolation + runtime probe for the native-runtime whole-agent container.
# Run INSIDE the container, e.g.:
#
#   docker run --rm --entrypoint /usr/local/bin/buzz-isolation-probe \
#     -e PROBE_HOST_CANARY=/home/ops/.buzz-isolation-canary buzz-native-agent:dev
#
# Checks:
#   1. effective uid is non-root (and matches PROBE_EXPECT_UID if set)
#   2. no docker socket (/var/run/docker.sock, /run/docker.sock), no DOCKER_HOST
#   3. a host path planted by the operator (PROBE_HOST_CANARY) is not visible,
#      and no bind mount originates from a host home (/home/*, /root)
#   4. git and ssh are present
#   5. each ACP adapter starts and answers an ACP `initialize` JSON-RPC request
#      over stdio (no model login needed) — both a plain v1 request and the
#      exact params buzz-acp sends
#
# Exit status: 0 if every check passed, 1 otherwise. Output is one line per
# check, prefixed PASS/FAIL/INFO.
set -uo pipefail

failures=0
pass() { printf 'PASS  %s\n' "$*"; }
fail() { printf 'FAIL  %s\n' "$*"; failures=$((failures + 1)); }
info() { printf 'INFO  %s\n' "$*"; }

# ── 1. identity ─────────────────────────────────────────────────────────────
uid="$(id -u)"
if [[ "$uid" == "0" ]]; then
    fail "running as root (id -u = 0)"
else
    pass "non-root: $(id)"
fi
if [[ -n "${PROBE_EXPECT_UID:-}" ]]; then
    if [[ "$uid" == "$PROBE_EXPECT_UID" ]]; then
        pass "uid matches expected $PROBE_EXPECT_UID"
    else
        fail "uid $uid != expected $PROBE_EXPECT_UID"
    fi
fi

# ── 2. docker socket ────────────────────────────────────────────────────────
sock_found=0
for sock in /var/run/docker.sock /run/docker.sock; do
    if [[ -e "$sock" ]]; then
        fail "docker socket present at $sock"
        sock_found=1
    fi
done
[[ $sock_found -eq 0 ]] && pass "no docker socket (/var/run/docker.sock, /run/docker.sock absent)"
if [[ -n "${DOCKER_HOST:-}" ]]; then
    fail "DOCKER_HOST is set ($DOCKER_HOST)"
else
    pass "DOCKER_HOST unset"
fi

# ── 3. host filesystem leakage ─────────────────────────────────────────────
canary="${PROBE_HOST_CANARY:-}"
if [[ -z "$canary" ]]; then
    fail "PROBE_HOST_CANARY not set — plant a file on the host and pass its path"
elif [[ -e "$canary" ]]; then
    fail "planted host path is visible inside the container: $canary"
else
    # A path under an unreadable dir (e.g. host /root, 0700) cannot be stat'ed
    # by this uid, so also fail if the canary or any ancestor is a mount point.
    ancestor="$canary"
    while [[ "$ancestor" != "/" && -n "$ancestor" ]]; do
        if awk -v p="$ancestor" '$5 == p {found=1} END {exit !found}' /proc/self/mountinfo; then
            fail "planted host path's ancestor $ancestor is a mount point"
            canary_mounted=1
            break
        fi
        ancestor="$(dirname "$ancestor")"
    done
fi
if [[ -n "$canary" && ! -e "$canary" && -z "${canary_mounted:-}" ]]; then
    pass "planted host path not visible (and no ancestor is a mount): $canary"
fi

# Bind mounts show their source-filesystem path in field 4 of mountinfo.
# Flag any mount whose source root lives under a host home directory.
home_mounts="$(awk '$4 ~ "^/(home|root)(/|$)" {print $4 " -> " $5}' /proc/self/mountinfo)"
if [[ -n "$home_mounts" ]]; then
    fail "mounts sourced from a host home directory:"
    printf '        %s\n' "$home_mounts"
else
    pass "no mount sourced from /home/* or /root"
fi
info "non-system mounts:"
awk '$5 !~ "^/(proc|sys|dev)(/|$)" && $5 != "/" {print "        " $5 " (" $4 ") " $6}' /proc/self/mountinfo

# ── 4. tools ────────────────────────────────────────────────────────────────
for tool in git ssh buzz-acp buzz node; do
    if path="$(command -v "$tool")"; then
        case "$tool" in
            git) ver="$(git --version)" ;;
            ssh) ver="$(ssh -V 2>&1)" ;;
            node) ver="$(node --version)" ;;
            *) if "$tool" --help >/dev/null 2>&1; then ver="--help ok"; else ver="--help FAILED"; fi ;;
        esac
        pass "$tool present: $path ($ver)"
    else
        fail "$tool not found on PATH"
    fi
done

# ── 5. ACP initialize over stdio ────────────────────────────────────────────
# Spawns the adapter, writes one newline-delimited JSON-RPC `initialize`, and
# waits for the response with id 1. stderr is captured for diagnostics only.
acp_initialize() {
    local cmd="$1" mode="${2:-v1}"
    node - "$cmd" "$mode" <<'NODE'
const { spawn } = require("node:child_process");
const cmd = process.argv[2];
const mode = process.argv[3];
const timeoutMs = Number(process.env.PROBE_ACP_TIMEOUT_MS || 30000);
const child = spawn(cmd, [], { stdio: ["pipe", "pipe", "pipe"] });
let out = "";
let err = "";
let done = false;
const finish = (code, msg) => {
  if (done) return;
  done = true;
  console.log(msg);
  try { child.kill("SIGTERM"); } catch {}
  setTimeout(() => process.exit(code), 200);
};
const timer = setTimeout(() => {
  finish(1, `timeout after ${timeoutMs}ms; stderr tail: ${err.slice(-400).replace(/\s+/g, " ")}`);
}, timeoutMs);
child.on("error", (e) => { clearTimeout(timer); finish(1, `spawn error: ${e.message}`); });
child.on("exit", (code, sig) => {
  clearTimeout(timer);
  finish(1, `exited early (code=${code} signal=${sig}); stderr tail: ${err.slice(-400).replace(/\s+/g, " ")}`);
});
child.stderr.on("data", (d) => { err += d; });
child.stdout.on("data", (d) => {
  out += d;
  let nl;
  while ((nl = out.indexOf("\n")) >= 0) {
    const line = out.slice(0, nl).trim();
    out = out.slice(nl + 1);
    if (!line) continue;
    let msg;
    try { msg = JSON.parse(line); } catch { continue; }
    if (msg.id !== 1) continue;
    clearTimeout(timer);
    if (msg.error) return finish(1, `error response: ${JSON.stringify(msg.error)}`);
    const r = msg.result || {};
    const agent = r.agentInfo ? `${r.agentInfo.name}@${r.agentInfo.version}` : "agentInfo n/a";
    const auth = (r.authMethods || []).map((m) => m.id).join(",") || "none";
    return finish(0, `protocolVersion=${r.protocolVersion} agent=${agent} authMethods=${auth}`);
  }
});
// "v1": a plain ACP v1 initialize. "buzz-acp": byte-for-byte the params
// buzz-acp sends (crates/buzz-acp/src/acp.rs build_initialize_params), which
// is what actually has to work for the harness to start the adapter.
const params = mode === "buzz-acp"
  ? {
      protocolVersion: 2,
      clientCapabilities: {
        auth: { terminal: true },
        _meta: { goose: { customNotifications: true }, "terminal-auth": true },
      },
      clientInfo: { name: "buzz-acp", version: "probe" },
    }
  : {
      protocolVersion: 1,
      clientCapabilities: { fs: { readTextFile: false, writeTextFile: false }, terminal: false },
      clientInfo: { name: "buzz-isolation-probe", version: "0" },
    };
child.stdin.write(JSON.stringify({ jsonrpc: "2.0", id: 1, method: "initialize", params }) + "\n");
NODE
}

for adapter in ${PROBE_ACP_ADAPTERS:-claude-agent-acp codex-acp}; do
    if ! command -v "$adapter" >/dev/null 2>&1; then
        fail "ACP adapter $adapter not found on PATH"
        continue
    fi
    for mode in v1 buzz-acp; do
        if result="$(acp_initialize "$adapter" "$mode")"; then
            pass "ACP initialize ($mode): $adapter -> $result"
        else
            fail "ACP initialize ($mode): $adapter -> $result"
        fi
    done
done

echo
if [[ $failures -eq 0 ]]; then
    echo "probe: all checks passed"
    exit 0
fi
echo "probe: $failures check(s) failed"
exit 1

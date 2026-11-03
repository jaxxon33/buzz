#!/bin/bash
# Entrypoint for the native-runtime whole-agent container.
#
# 1. Loads the agent's env file from a read-only bind mount (default
#    /run/agent/env). Lines are parsed as KEY=VALUE and exported verbatim —
#    never `source`d, so the file cannot execute code. This keeps secrets out
#    of `docker inspect` (unlike `docker run --env-file`).
# 2. Points git/ssh at the agent's own read-only key and known_hosts, if mounted.
# 3. Scopes the Nostr git credential helper to the relay URL (if installed).
# 4. execs buzz-acp (run with `docker run --init`: tini is PID 1 and forwards signals).
set -euo pipefail

env_file="${BUZZ_AGENT_ENV_FILE:-/run/agent/env}"
if [[ -f "$env_file" ]]; then
    while IFS= read -r line || [[ -n "$line" ]]; do
        line="${line%$'\r'}"
        [[ -z "$line" || "$line" == \#* ]] && continue
        line="${line#export }"
        key="${line%%=*}"
        value="${line#*=}"
        if [[ ! "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ || "$key" == "$line" ]]; then
            echo "buzz-native-entrypoint: skipping malformed env line" >&2
            continue
        fi
        # Strip one layer of matching surrounding quotes.
        if [[ "$value" =~ ^\"(.*)\"$ || "$value" =~ ^\'(.*)\'$ ]]; then
            value="${BASH_REMATCH[1]}"
        fi
        export "$key=$value"
    done <"$env_file"
fi

# Agent's own SSH identity (read-only mount). ssh refuses keys readable by
# others, so the host must install it owned by the agent uid with mode 0400.
ssh_dir="${BUZZ_AGENT_SSH_DIR:-/run/agent/ssh}"
if [[ -z "${GIT_SSH_COMMAND:-}" && -d "$ssh_dir" ]]; then
    key=""
    for candidate in id_ed25519 id_ecdsa id_rsa; do
        if [[ -f "$ssh_dir/$candidate" ]]; then
            key="$ssh_dir/$candidate"
            break
        fi
    done
    if [[ -n "$key" ]]; then
        known_hosts="$ssh_dir/known_hosts"
        export GIT_SSH_COMMAND="ssh -i $key -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes -o UserKnownHostsFile=$known_hosts"
    fi
fi

if [[ -n "${BUZZ_RELAY_URL:-}" ]] && command -v git-credential-nostr >/dev/null 2>&1; then
    relay_http_url="${BUZZ_RELAY_URL/#ws:/http:}"
    relay_http_url="${relay_http_url/#wss:/https:}"
    relay_http_url="${relay_http_url%/}"
    git config --global "credential.${relay_http_url}/git.helper" \
        "$(command -v git-credential-nostr)"
    git config --global "credential.${relay_http_url}/git.useHttpPath" true
fi

exec buzz-acp "$@"

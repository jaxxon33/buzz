# The host stages only the four existing runtime binaries and public instructions.
FROM python:3.12-slim@sha256:229a2c5bfa27522db7815ea81f9bed70af17ccb9de9fc7ad142b1877b5830d36
ARG WORKER_UID=61021
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates git ripgrep tree procps openssh-client \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid ${WORKER_UID} buzz-worker \
    && useradd --uid ${WORKER_UID} --gid ${WORKER_UID} --home-dir /workspace --no-create-home buzz-worker \
    && mkdir /workspace && chown ${WORKER_UID}:${WORKER_UID} /workspace
COPY --chmod=0555 bin/ /opt/buzz/bin/
COPY --chmod=0444 base-prompt.md /opt/buzz/base-prompt.md
COPY --chmod=0444 contained.py relay_tunnel.py /opt/buzz/
COPY --chmod=0444 ssh/known_hosts /opt/buzz/ssh/known_hosts
COPY --chmod=0444 ssh/config /etc/ssh/ssh_config.d/90-buzz-agent.conf
RUN chmod 0555 /opt/buzz/ssh
ENV HOME=/workspace PATH=/opt/buzz/bin:/usr/local/bin:/usr/bin:/bin \
    BUZZ_NEST=/workspace BUZZ_ACP_AGENT_COMMAND=/opt/buzz/bin/buzz-agent \
    BUZZ_ACP_AGENT_ARGS="" BUZZ_ACP_MCP_COMMAND=/opt/buzz/bin/buzz-dev-mcp \
    BUZZ_ACP_BASE_PROMPT_FILE=/opt/buzz/base-prompt.md
USER ${WORKER_UID}:${WORKER_UID}
WORKDIR /workspace
ENTRYPOINT ["python3", "/opt/buzz/contained.py"]

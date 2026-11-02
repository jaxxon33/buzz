# The host stages only the four existing runtime binaries and public instructions.
FROM python:3.12-slim@sha256:229a2c5bfa27522db7815ea81f9bed70af17ccb9de9fc7ad142b1877b5830d36
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates git ripgrep tree procps \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 61021 buzz-worker \
    && useradd --uid 61021 --gid 61021 --home-dir /workspace --no-create-home buzz-worker \
    && mkdir /workspace && chown 61021:61021 /workspace
COPY --chmod=0555 bin/ /opt/buzz/bin/
COPY --chmod=0444 base-prompt.md /opt/buzz/base-prompt.md
COPY --chmod=0444 contained.py relay_tunnel.py /opt/buzz/
ENV HOME=/workspace PATH=/opt/buzz/bin:/usr/local/bin:/usr/bin:/bin \
    BUZZ_NEST=/workspace BUZZ_ACP_AGENT_COMMAND=/opt/buzz/bin/buzz-agent \
    BUZZ_ACP_AGENT_ARGS="" BUZZ_ACP_MCP_COMMAND=/opt/buzz/bin/buzz-dev-mcp \
    BUZZ_ACP_BASE_PROMPT_FILE=/opt/buzz/base-prompt.md
USER 61021:61021
WORKDIR /workspace
ENTRYPOINT ["python3", "/opt/buzz/contained.py"]

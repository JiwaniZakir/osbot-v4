# Stage 1: Install Claude Code CLI (needs Node.js)
FROM node:18.20-slim AS claude-cli
RUN npm install -g @anthropic-ai/claude-code@latest

# Stage 2: Runtime (Python 3.12 + Claude CLI from stage 1)
FROM python:3.12-slim

# Copy Claude CLI from stage 1
COPY --from=claude-cli /usr/local/lib/node_modules /usr/local/lib/node_modules
COPY --from=claude-cli /usr/local/bin/node /usr/local/bin/node
RUN ln -sf /usr/local/lib/node_modules/@anthropic-ai/claude-code/cli.js /usr/local/bin/claude && \
    chmod +x /usr/local/bin/claude

# System deps: git, gh CLI
RUN apt-get update && apt-get install -y --no-install-recommends \
    git curl gpg ca-certificates && \
  curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg | \
    gpg --dearmor -o /usr/share/keyrings/githubcli-archive-keyring.gpg && \
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" | \
    tee /etc/apt/sources.list.d/github-cli.list > /dev/null && \
  apt-get update && apt-get install -y gh && \
  apt-get clean && rm -rf /var/lib/apt/lists/*

# Non-root user
RUN useradd -m -s /bin/bash botuser

WORKDIR /opt/osbot

# Python deps + Agent SDK + timezone data
RUN pip install --no-cache-dir \
    pydantic pydantic-settings structlog httpx aiosqlite tzdata \
    claude-agent-sdk

# Copy source
COPY src/ ./src/

# Runtime directories (session-env required for Claude CLI Bash tool to work)
RUN mkdir -p state workspaces /home/botuser/.claude/session-env /home/botuser/.config/gh

# Default Claude settings
RUN echo '{"model":"claude-sonnet-4-6"}' > /home/botuser/.claude/settings.json

# Health check — liveness-gated via state/heartbeat.json written by the
# orchestrator every cycle. Start-period gives the first cycle time to
# write the initial heartbeat before failures start counting.
COPY deploy/health_check.py ./deploy/health_check.py
HEALTHCHECK --interval=60s --timeout=10s --retries=3 --start-period=120s \
  CMD python3 /opt/osbot/deploy/health_check.py

# Entrypoint
COPY deploy/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENV PYTHONPATH=/opt/osbot/src

RUN chown -R botuser:botuser /opt/osbot /home/botuser
USER botuser

ENTRYPOINT ["/entrypoint.sh"]
CMD ["python3", "-m", "osbot"]

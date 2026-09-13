# Multi-stage Dockerfile for JeanClode CLI Worker
# Optimized for isolated container execution from backend
#
# Build context must be cli/ with plugins copied in:
#   make cli-build  (handles this automatically)

## Builder stage — installs deps and CLI tools, then discarded
FROM python:3.14-slim AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates gnupg git && rm -rf /var/lib/apt/lists/*

# GitHub CLI
RUN curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
    -o /usr/share/keyrings/githubcli-archive-keyring.gpg && \
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
    > /etc/apt/sources.list.d/github-cli.list && \
    apt-get update && apt-get install -y --no-install-recommends gh && rm -rf /var/lib/apt/lists/*

# GitLab CLI
ARG GLAB_VERSION=1.88.0
RUN curl -fsSL "https://gitlab.com/gitlab-org/cli/-/releases/v${GLAB_VERSION}/downloads/glab_${GLAB_VERSION}_linux_$(dpkg --print-architecture).deb" \
    -o /tmp/glab.deb && dpkg -i /tmp/glab.deb && rm /tmp/glab.deb

# Python deps
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
RUN useradd -m -u 1000 jeanclode
COPY --chown=jeanclode:jeanclode pyproject.toml uv.lock /app/
RUN cd /app && uv sync --frozen --no-dev --no-install-project


## Runtime stage — only what's needed to run
FROM python:3.14-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    git ca-certificates curl unzip && rm -rf /var/lib/apt/lists/*

# Bun + Claude Code CLI (SDK spawns claude-code as subprocess)
ARG BUN_VERSION=1.2.5
RUN DPKG_ARCH=$(dpkg --print-architecture) && \
    case "$DPKG_ARCH" in arm64) BUN_ARCH="aarch64";; amd64) BUN_ARCH="x64";; *) echo "Unsupported arch: $DPKG_ARCH" >&2; exit 1;; esac && \
    curl -fsSL --retry 3 "https://github.com/oven-sh/bun/releases/download/bun-v${BUN_VERSION}/bun-linux-${BUN_ARCH}.zip" \
    -o /tmp/bun.zip && \
    unzip -q /tmp/bun.zip -d /tmp && \
    mv /tmp/bun-linux-${BUN_ARCH}/bun /usr/local/bin/bun && \
    chmod +x /usr/local/bin/bun && \
    rm -rf /tmp/bun.zip /tmp/bun-linux-* && \
    BUN_INSTALL=/usr/local bun install -g @anthropic-ai/claude-code

# Copy CLI tools from builder
COPY --from=builder /usr/bin/gh /usr/bin/gh
COPY --from=builder /usr/bin/glab /usr/bin/glab

# Non-root user
RUN useradd -m -u 1000 jeanclode
USER jeanclode
WORKDIR /home/jeanclode

# Copy venv from builder
COPY --from=builder --chown=jeanclode:jeanclode /app/.venv /app/.venv

# Copy source + plugins (copied into cli/ by make cli-build)
COPY --chown=jeanclode:jeanclode . /app/

# Make `jeanclode` available as a command for CI runners
RUN printf '#!/bin/sh\nmkdir -p /tmp/.claude\necho '"'"'{"hasCompletedOnboarding":true}'"'"' > /tmp/.claude.json\nexport HOME=/tmp\nulimit -s unlimited\nexec python /app/src/main.py "$@"\n' > /app/.venv/bin/jeanclode && chmod +x /app/.venv/bin/jeanclode

ENV JEANCLODE_CONTAINER_MODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH="/app" \
    PATH="/app/.venv/bin:${PATH}" \
    GH_CONFIG_DIR="/tmp/gh" \
    GLAB_CONFIG_DIR="/tmp/glab"

ENTRYPOINT ["sh", "-c", "mkdir -p /tmp/.claude && echo '{\"hasCompletedOnboarding\":true}' > /tmp/.claude.json && export HOME=/tmp && ulimit -s unlimited && exec python /app/src/main.py \"$@\"", "--"]

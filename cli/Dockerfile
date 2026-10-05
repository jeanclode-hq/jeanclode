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

# Demo gate (issue-resolve). Early and pinned so nodes keep these layers across
# releases. Chromium headless shell records the demo; PLAYWRIGHT_VERSION must
# match the `playwright` pin in pyproject.toml, which drives it.
ARG PLAYWRIGHT_VERSION=1.63.0
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
RUN pip install --no-cache-dir "playwright==${PLAYWRIGHT_VERSION}" && \
    playwright install --with-deps --only-shell chromium && \
    pip uninstall -y playwright && \
    rm -rf /var/lib/apt/lists/* && chmod -R a+rX /ms-playwright

# mise installs whatever Node a repo pins, at run time. No Node is preinstalled:
# most frontends pin their own, and every other workflow would carry it for nothing.
# Its dirs are under /tmp: the root filesystem is read-only at run time.
ARG MISE_VERSION=v2026.10.2
ENV MISE_DATA_DIR=/tmp/mise \
    MISE_CONFIG_DIR=/tmp/mise/config \
    MISE_CACHE_DIR=/tmp/mise/cache \
    MISE_STATE_DIR=/tmp/mise/state
RUN curl -fsSL https://mise.run | MISE_VERSION=${MISE_VERSION} MISE_INSTALL_PATH=/usr/local/bin/mise sh

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
    PATH="/app/.venv/bin:/tmp/mise/shims:${PATH}" \
    GH_CONFIG_DIR="/tmp/gh" \
    GLAB_CONFIG_DIR="/tmp/glab"

ENTRYPOINT ["sh", "-c", "mkdir -p /tmp/.claude && echo '{\"hasCompletedOnboarding\":true}' > /tmp/.claude.json && export HOME=/tmp && ulimit -s unlimited && exec python /app/src/main.py \"$@\"", "--"]

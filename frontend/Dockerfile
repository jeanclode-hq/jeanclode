# Multi-stage Dockerfile for Jeanclode Frontend (Nuxt 4 SSR with pnpm workspace)

# ===========================
# Stage 1: Builder
# ===========================
FROM dhi.io/node:24-alpine3.23-dev AS builder

# Install pnpm
RUN corepack enable && corepack prepare pnpm@10.21.0 --activate

# Set working directory to workspace root
WORKDIR /workspace

# Copy workspace configuration files
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./

# Copy workspace packages (api-types is needed by frontend)
COPY packages ./packages

# Copy frontend app
COPY frontend ./frontend

# Configure pnpm to hoist dependencies for proper Tailwind CSS resolution
RUN pnpm config set shamefully-hoist true

# Install only frontend's dependencies (and its workspace deps, e.g. api-types).
# Scoped so the website workspace member's deps (e.g. better-sqlite3, which needs
# a native build and isn't needed here) never enter this image.
RUN pnpm install --frozen-lockfile --filter jeanclode-frontend...

# Prepare Nuxt (generate types, etc.)
RUN cd frontend && pnpm run postinstall

# Build the frontend app
# Note: NUXT_PUBLIC_* env vars are read at RUNTIME by the Nuxt SSR server,
# not at build time. Set them when running the container.
RUN pnpm --filter jeanclode-frontend run build

# ===========================
# Stage 2: Runtime
# ===========================
FROM dhi.io/node:24-alpine3.23 AS runtime

WORKDIR /app

# Copy built application from builder with correct permissions (world-readable for k8s security context)
COPY --from=builder --chmod=755 /workspace/frontend/.output /app/.output
COPY --from=builder --chmod=644 /workspace/frontend/package.json /app/

# Switch to non-root user (for local docker use; k8s may override via securityContext)
USER 65534

# Expose port
EXPOSE 3000

# Exec form with node: the hardened runtime image has no shell and no wget.
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD ["node", "-e", "fetch('http://127.0.0.1:3000/').then((r) => process.exit(r.status < 500 ? 0 : 1)).catch(() => process.exit(1))"]

# Set environment variables
ENV HOST=0.0.0.0
ENV PORT=3000
ENV NODE_ENV=production

# Start Nuxt server
CMD ["node", ".output/server/index.mjs"]

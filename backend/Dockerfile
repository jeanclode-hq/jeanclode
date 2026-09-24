# Multi-stage Dockerfile for Jeanclode Backend (FastAPI + Python 3.14)

# ===========================
# Stage 1: Builder
# ===========================
FROM python:3.14-slim AS builder

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    g++ \
    make \
    libpq-dev \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install uv (fast Python package installer)
RUN pip install --no-cache-dir uv

# Set working directory
WORKDIR /app

# Copy dependency files
COPY pyproject.toml uv.lock ./

# Install exactly what uv.lock pins; `uv pip install` ignores the lockfile and floats
# (SQLAlchemy 2.1 switched the default postgresql:// driver to psycopg v3).
RUN uv export --frozen --no-dev --no-emit-project -o /tmp/requirements.txt \
    && uv pip install --system --no-cache -r /tmp/requirements.txt

# ===========================
# Stage 2: Runtime
# ===========================
FROM python:3.14-slim AS runtime

# Install runtime dependencies only
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user for security
RUN groupadd -g 999 docker && \
    useradd -m -u 1000 -G docker jeanclode

# Set working directory
WORKDIR /app

# Copy installed packages from builder
COPY --from=builder /usr/local/lib/python3.14/site-packages /usr/local/lib/python3.14/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# Copy application code
COPY . .
RUN chmod -R a+rX /app

# Switch to non-root user
USER jeanclode

# Expose port
EXPOSE 8000

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

# Start server
CMD ["python", "run.py"]

# syntax=docker/dockerfile:1.7
FROM python:3.13-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN pip install --no-cache-dir "mitmproxy==11.1.3" "pydantic>=2.7,<3"

WORKDIR /app
COPY proxy.py /app/proxy.py
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh \
    && useradd --system --uid 65532 --no-create-home secproxy

USER 65532:65532

ENTRYPOINT ["/entrypoint.sh"]

#!/bin/sh
# Combine ca.crt + ca.key into mitmproxy's expected mitmproxy-ca.pem.
# http2 disabled — H2 connection coalescing breaks *.github.com wildcard reuse.
# ssl_insecure — self-hosted LLM endpoints (e.g. an internal GPU node) are often
# behind a private/internal CA the sidecar's trust store doesn't know about;
# without this the upstream TLS handshake fails with "unable to get local
# issuer certificate". The per-execution host allowlist in proxy.py is the
# real security boundary here, not upstream cert verification.
set -eu

CA_DIR="${SECURITY_PROXY_CA_DIR:-/etc/security-proxy}"
CONFDIR="${SECURITY_PROXY_MITM_CONFDIR:-/tmp/mitm}"
BIND="${SECURITY_PROXY_BIND:-127.0.0.1:8080}"
LISTEN_HOST="${BIND%:*}"
LISTEN_PORT="${BIND##*:}"

mkdir -p "$CONFDIR"
cat "$CA_DIR/ca.crt" "$CA_DIR/ca.key" > "$CONFDIR/mitmproxy-ca.pem"
chmod 600 "$CONFDIR/mitmproxy-ca.pem"

exec mitmdump \
    --listen-host "$LISTEN_HOST" \
    --listen-port "$LISTEN_PORT" \
    --set "confdir=$CONFDIR" \
    --set http2=false \
    --set termlog_verbosity=warn \
    --set flow_detail=0 \
    --set ssl_insecure=true \
    --scripts /app/proxy.py

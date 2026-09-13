# Sandbox / Security Proxy

Every container runs sandboxed: the agent reaches the network only through a per-execution mitmproxy sidecar that enforces a strict host allowlist and injects credentials on outbound requests. **Real credentials never reach the agent container** — they live in `secrets`, which only the sidecar can read. The agent runs token-less by design.

Reference implementations:
- Backend: `backend/api/plugins/sentry/launch.py` (`build_dispatch_inputs`, `_add_*` helpers)
- Proxy: `backend/api/plugins/container/security_proxy.py` (`UpstreamCredential`, `CredentialKey`, `build_proxy_spec`)
- Sidecar: `security-proxy/proxy.py` (header strip/inject, response scrub)

## What goes in the dispatch inputs

The launcher builds and hands the K8s backend explicit dispatch inputs:

```python
class _DispatchInputs(BaseModel):
    public_env: dict[str, str]           # Reaches the agent. Hostnames, model names, non-secret config.
    secrets: dict[str, str]              # Sidecar only. Real token values keyed by env var name.
    upstreams: list[UpstreamCredential]  # One per (host, header) injection rule.
    extra_hosts: list[str]               # Hosts allowed without auth injection (public CDNs).
```

Hard rule: a credential value never appears in `public_env`. If the agent shouldn't see it, it goes in `secrets` and the proxy injects it on the relevant host.

## Declaring an upstream

For every external host the agent's tooling reaches, emit one `UpstreamCredential`:

```python
inputs.secrets[CredentialKey.LINEAR_API_KEY] = token
inputs.upstreams.append(
    UpstreamCredential(
        secret_key=CredentialKey.LINEAR_API_KEY,
        host="api.linear.app",
        header="Authorization",
        bearer=True,   # injects "Authorization: Bearer ${LINEAR_API_KEY}"
    )
)
```

- `bearer=True` → `Authorization: Bearer ${VAR}`. Standard for most APIs.
- `bearer=False` with `header="Authorization"` → bare `${VAR}` (e.g., GitLab uses `Basic <b64>` for git-over-HTTP).
- `bearer=False` with custom header (e.g., `header="x-api-key"` for Anthropic, `header="PRIVATE-TOKEN"` for GitLab API).

Multiple `UpstreamCredential` entries on the same host coalesce into one upstream with all headers injected. GitLab is the canonical example: `PRIVATE-TOKEN` for `/api/v4/*` and `Authorization: Basic` for git smart HTTP, both on `gitlab.com`.

## Hosts without auth (`extra_hosts`)

Public CDNs and unauthenticated endpoints the agent's tooling fetches from go in `extra_hosts`. They're allowlisted but no header injection happens. Example: `raw.githubusercontent.com`, `objects.githubusercontent.com`, plugin marketplaces.

## Region / SaaS auto-routing

Some SaaS (Sentry is the example) auto-redirects clients to a regional API host (`us.sentry.io`, `de.sentry.io`, `eu.sentry.io`). The agent hits the global host first, learns the region from the response, then re-targets — so **all candidate region hosts must be in the allowlist up front**. See `_SENTRY_SAAS_HOSTS` in `sentry/launch.py`.

## Self-hosted endpoints

When the org is on a self-hosted instance, the host comes from the org row's `base_url`, not a constant. Use `urlparse(...).hostname` (or `_host_or` in `sentry/launch.py`) to resolve and pass that as the upstream host. The same code path covers SaaS and self-hosted with no special-casing.

## Header strip list

The proxy strips these headers from every request before injecting (so a buggy client can't smuggle real creds past the policy):

```
authorization, proxy-authorization, x-api-key, anthropic-api-key, private-token
```

If your integration uses a non-standard credential header (e.g., a custom `X-MyService-Token`), add it to `STRIPPED_HEADERS` in `security-proxy/proxy.py`. Otherwise the placeholder value the CLI sends will leak through unchanged.

The same set plus `set-cookie` is stripped from upstream **responses** so a buggy server echoing creds back can't leak them to the agent.

## CLI-side: placeholder token

In container mode, `cli/src/runner/preflight.py:authenticate()` returns the string `"sandbox-placeholder"` when no real token is in the agent's env. Tools like `gh`/`glab`/`curl` refuse to send a request without a non-empty token; the placeholder makes them willing, the proxy strips and rewrites with the real value on the way out.

If your integration uses a CLI tool that has its own quirks about empty tokens, verify it still sends a request when `<TOOL>_TOKEN=sandbox-placeholder`. Most do.

## Verifying end-to-end

To verify a new upstream actually works, spin the proxy up locally:

```bash
cd security-proxy
python3 path/to/upstream.py &                # or use a real allowed host
SECURITY_PROXY_CONFIG=/path/to/config.json \
  REAL_TOKEN=test-token \
  uv run mitmdump --listen-host 127.0.0.1 --listen-port 8080 \
  --set http2=false --scripts ./proxy.py &
curl -sS -x http://127.0.0.1:8080 -D - http://your-host/path
```

Confirm the upstream sees `Authorization: Bearer test-token` (proxy injected) and the response that reaches `curl` has no credential headers.

## Checklist

- [ ] `launch.py` builds `_DispatchInputs` with `public_env`, `secrets`, `upstreams`, `extra_hosts`
- [ ] Real credential values live only in `secrets`, never in `public_env`
- [ ] Every external host the agent reaches has either an `UpstreamCredential` or an `extra_hosts` entry
- [ ] SaaS region hosts (if applicable) are all allowlisted up front
- [ ] Self-hosted base URL is parsed to host (don't hardcode)
- [ ] Multi-header injection on the same host is split into separate `UpstreamCredential` entries (they coalesce)
- [ ] Non-standard credential headers added to `STRIPPED_HEADERS` in the proxy
- [ ] CLI tools used by the workflow tolerate `<TOOL>_TOKEN=sandbox-placeholder`
- [ ] End-to-end verified against a local proxy with a known-injected token

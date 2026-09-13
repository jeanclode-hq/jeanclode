"""Build per-execution sandbox material for the security-proxy sidecar.

This module is intentionally **dumb infrastructure**. It does not look at the
DB, admin settings, or any environment to discover which hosts are allowed.
The dispatching plugin already knows that — LLM hosts come from admin LLM
config or env, git hosts come from admin settings or env, integration hosts
(Sentry, Linear, …) come from the org row being dispatched. The plugin
resolves all of that and passes ``UpstreamCredential`` entries here
explicitly. Two benefits: (1) one source of truth per credential lives in
the plugin that owns it, (2) supporting a new self-hosted upstream is
zero-change in this file — the plugin just emits a different host.

Outputs:
  * a JSON config file (templated; only ``${VAR}`` references for secrets)
  * a freshly minted self-signed CA (cert + key)
  * the secret env mapping the sidecar runs under

The CA cert+key go to the sidecar. Only the cert reaches the agent's trust
store.
"""

import datetime as dt
import json
import secrets
import uuid
from collections import defaultdict
from typing import Final

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from pydantic import BaseModel

from api.plugins.container.schema import ProxySpec


class UpstreamCredential(BaseModel):
    """One injected credential, resolved by the calling plugin.

    Attributes:
        secret_key: Env var name that holds the credential value on the
            sidecar (e.g. ``SENTRY_AUTH_TOKEN``).
        host: Resolved upstream host (e.g. ``sentry.acme.com`` for a
            self-hosted Sentry, or ``api.openai.com`` for default OpenAI).
            The plugin computes this from whatever sources it has.
        header: HTTP header to inject the credential into. Almost always
            ``Authorization``; Anthropic uses ``x-api-key``.
        bearer: When True, value is templated as ``Bearer ${VAR}``;
            otherwise the bare reference (``${VAR}``) is injected.
        path_prefix: When set, the proxy injects this credential only for
            requests whose URL path starts with this prefix. Used for
            GitLab cross-namespace token routing: each org's token is
            scoped to its own namespace path (e.g. ``/company/backend/``).
            Entries on the same host with different prefixes coalesce into
            separate upstream objects. Entries without a prefix continue
            to match all paths on that host (GitHub, Sentry, LLM).
    """

    secret_key: str
    host: str
    header: str
    bearer: bool = False
    path_prefix: str | None = None


class OAuthUpstream(BaseModel):
    """One oauth2 token-minting injection rule, minted proxy-side.

    Unlike ``UpstreamCredential``, this isn't static ``${VAR}`` templating —
    the sidecar actively calls ``token_url`` itself (independent of any
    agent-initiated request), caches the result by its ``expires_in``, and
    injects ``Bearer <minted token>`` into matching requests. The backend
    never touches the actual access token, only the credential fields
    below.

    Attributes:
        client_id_key/client_secret_key: Env var names on the sidecar
            holding the (non-rotating) OAuth2 client credentials — same
            ``secret_env`` indirection as ``UpstreamCredential.secret_key``,
            so these never appear in the config JSON in the clear. Optional
            because some providers accept a public client on the
            password/refresh_token grants.
        username_key/password_key: Same indirection, populated only for
            the ``password`` grant.
        refresh_token_key: Same indirection, populated only for the
            ``refresh_token`` grant.
        host/header/path_prefix: Where the minted token gets injected —
            same semantics as ``UpstreamCredential``.
        token_url: The provider's OAuth2 token endpoint. A *different*
            host from ``host`` above — one mints the token, the other is
            where it's used.
    """

    client_id_key: str | None = None
    client_secret_key: str | None = None
    username_key: str | None = None
    password_key: str | None = None
    refresh_token_key: str | None = None
    host: str
    header: str = "Authorization"
    path_prefix: str | None = None
    token_url: str
    grant_type: str = "client_credentials"
    scope: str | None = None


def build_proxy_spec(
    *,
    secret_env: dict[str, str],
    upstreams: list[UpstreamCredential],
    image: str,
    execution_id: str | uuid.UUID,
    extra_hosts: list[str] | None = None,
    oauth_upstreams: list[OAuthUpstream] | None = None,
) -> ProxySpec:
    """Build a ``ProxySpec`` from explicit per-credential resolution.

    ``secret_env`` is the credential values keyed by env var name; these
    sit on the sidecar. ``upstreams`` is the matching allowlist with
    injection metadata — order doesn't matter, multiple entries on the
    same host coalesce. ``extra_hosts`` adds hosts to the allowlist
    *without* any auth injection (e.g., public CDNs the agent's tooling
    fetches from).

    The sidecar's allowlist is exactly the union of those two sets;
    every credential without an entry stays opaque on the sidecar but
    won't be auto-injected anywhere.
    """
    config_json = json.dumps(
        {
            "execution_id": str(execution_id),
            "upstreams": _serialize_upstreams(upstreams, extra_hosts or []),
            "oauth_upstreams": _serialize_oauth_upstreams(oauth_upstreams or []),
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    cert_pem, key_pem = mint_ca(execution_id=str(execution_id))
    return ProxySpec(
        config_json=config_json,
        ca_cert_pem=cert_pem,
        ca_key_pem=key_pem,
        secret_env=secret_env,
        image=image,
    )


def _serialize_upstreams(
    upstreams: list[UpstreamCredential],
    extra_hosts: list[str],
) -> list[dict[str, object]]:
    """Coalesce per-(host, path_prefix) injection rules into the sidecar's wire format.

    Two ``UpstreamCredential`` entries on the same host *and* same
    ``path_prefix`` produce one upstream object with both headers set.
    Entries with different ``path_prefix`` values (e.g. two GitLab orgs
    on the same host) produce separate objects so the proxy can route each
    token to the correct namespace. ``extra_hosts`` are emitted with empty
    inject maps so the proxy allows them through without modifying headers.
    """
    by_key: dict[tuple[str, str | None], dict[str, str]] = defaultdict(dict)
    for rule in upstreams:
        value = f"Bearer ${{{rule.secret_key}}}" if rule.bearer else f"${{{rule.secret_key}}}"
        by_key[(rule.host, rule.path_prefix)][rule.header] = value

    for host in extra_hosts:
        by_key.setdefault((host, None), {})

    result = []
    for (host, path_prefix), headers in sorted(
        by_key.items(), key=lambda item: (item[0][0], item[0][1] or "")
    ):
        entry: dict[str, object] = {"host": host, "inject": dict(sorted(headers.items()))}
        if path_prefix is not None:
            entry["path_prefix"] = path_prefix
        result.append(entry)
    return result


def _serialize_oauth_upstreams(oauth_upstreams: list[OAuthUpstream]) -> list[dict[str, object]]:
    """Same ``${VAR}`` indirection as ``_serialize_upstreams`` — ``client_id``/
    ``client_secret`` reference ``secret_env`` keys, never appearing in the
    config JSON in the clear.
    """
    result: list[dict[str, object]] = []
    for u in oauth_upstreams:
        entry: dict[str, object] = {
            "host": u.host,
            "header": u.header,
            "token_url": u.token_url,
            "grant_type": u.grant_type,
        }
        if u.client_id_key is not None:
            entry["client_id"] = f"${{{u.client_id_key}}}"
        if u.client_secret_key is not None:
            entry["client_secret"] = f"${{{u.client_secret_key}}}"
        if u.username_key is not None:
            entry["username"] = f"${{{u.username_key}}}"
        if u.password_key is not None:
            entry["password"] = f"${{{u.password_key}}}"
        if u.refresh_token_key is not None:
            entry["refresh_token"] = f"${{{u.refresh_token_key}}}"
        if u.path_prefix is not None:
            entry["path_prefix"] = u.path_prefix
        if u.scope is not None:
            entry["scope"] = u.scope
        result.append(entry)
    return result


def mint_ca(*, execution_id: str) -> tuple[str, str]:
    """Mint a short-lived self-signed CA for one execution.

    Returns ``(cert_pem, key_pem)``. ECDSA P-256 is the sweet spot —
    every modern TLS client (rustls, OpenSSL, Node, Go) accepts the
    resulting leaf certs out of the box. Ed25519 looked tempting but
    Node TLS clients drop the connection with
    ``NoSignatureSchemesInCommon`` against an Ed25519-issued leaf.
    """
    key = ec.generate_private_key(ec.SECP256R1())
    serial = int.from_bytes(secrets.token_bytes(20), "big") >> 1  # positive 159-bit
    now = dt.datetime.now(dt.UTC)
    name = x509.Name(
        [
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Jeanclode"),
            x509.NameAttribute(
                NameOID.COMMON_NAME,
                f"security-proxy-{execution_id[:32]}",
            ),
        ]
    )
    public_key = key.public_key()
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(public_key)
        .serial_number(serial)
        .not_valid_before(now - dt.timedelta(minutes=1))
        .not_valid_after(now + dt.timedelta(hours=12))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                key_cert_sign=True,
                crl_sign=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        # SKI is mandatory on CA certs per RFC 5280 — without it,
        # OpenSSL rejects rcgen-issued leaves with "Missing Authority Key
        # Identifier" because rcgen can't derive AKI from a CA that has no SKI.
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(public_key),
            critical=False,
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(public_key),
            critical=False,
        )
        .sign(key, algorithm=hashes.SHA256())
    )

    cert_pem = cert.public_bytes(serialization.Encoding.PEM).decode("utf-8")
    key_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")
    return cert_pem, key_pem


# Re-exported for convenience: _Final[Final]_ list of well-known credential env
# var names. Callers building ``UpstreamCredential`` entries usually reference
# these constants rather than typing the strings.
class CredentialKey:
    ANTHROPIC_API_KEY: Final[str] = "ANTHROPIC_API_KEY"
    CLAUDE_CODE_OAUTH_TOKEN: Final[str] = "CLAUDE_CODE_OAUTH_TOKEN"
    OPENAI_API_KEY: Final[str] = "OPENAI_API_KEY"
    # gh CLI's standard env var name for installation tokens.
    GH_TOKEN: Final[str] = "GH_TOKEN"
    GITLAB_TOKEN: Final[str] = "GITLAB_TOKEN"
    SENTRY_AUTH_TOKEN: Final[str] = "SENTRY_AUTH_TOKEN"
    MEMORY_API_TOKEN: Final[str] = "MEMORY_API_TOKEN"

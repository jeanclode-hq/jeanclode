"""mitmproxy addon for the per-execution security sidecar."""

import asyncio
import html
import json
import os
import re
import secrets
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, quote_plus, unquote, urlencode

import pyotp
from mitmproxy import http
from pydantic import BaseModel, Field

DEFAULT_CONFIG_PATH = "/etc/security-proxy-config/config.json"

# Minted tokens are refreshed this many seconds before their reported
# expiry, so a token handed out right at the edge of its lifetime doesn't
# expire mid-flight on the request it was minted for.
OAUTH_TOKEN_REFRESH_MARGIN_SECONDS = 30
OAUTH_TOKEN_REQUEST_TIMEOUT_SECONDS = 10

STRIPPED_HEADERS: tuple[str, ...] = (
    "authorization",
    "proxy-authorization",
    "x-api-key",
    "anthropic-api-key",
    "private-token",
)

# Response-side scrub. Servers don't normally echo Authorization, but a buggy
# upstream or a user-controlled body field could leak credentials back. Strip
# anything credential-shaped on the way to the agent as defense-in-depth.
# ``set-cookie`` isn't stripped but vaulted: see ``_CookieVault``.
STRIPPED_RESPONSE_HEADERS: tuple[str, ...] = STRIPPED_HEADERS

# Login payloads are tiny; anything bigger goes out unscanned rather than
# making every upload to a body-secret host a full in-memory copy.
MAX_BODY_SUBSTITUTION_BYTES = 64 * 1024

COOKIE_PLACEHOLDER_PREFIX = "jccookie_"
# One sidecar per execution; the cap only bounds a run that churns cookies.
MAX_VAULTED_COOKIES = 4096


class Upstream(BaseModel):
    host: str
    path_prefix: str | None = None
    # A second kind of rule, for paths no prefix can scope: matched against the
    # raw path, and only for ``methods``. GitLab's npm registry is the case —
    # its metadata path carries no project, and its tarball paths name the
    # package's project, which this run doesn't track.
    path_pattern: str | None = None
    methods: list[str] | None = None
    inject: dict[str, str] = Field(default_factory=dict)
    # Placeholder -> real secret, swapped into request bodies on this host.
    body: dict[str, str] = Field(default_factory=dict)
    # Placeholder -> ``otpauth://`` URI; swapped for the code current at send time.
    totp: dict[str, str] = Field(default_factory=dict)

    def matches_pattern(self, method: str, path: str) -> bool:
        if self.path_pattern is None:
            return False
        if self.methods is not None and method.upper() not in self.methods:
            return False
        return re.search(self.path_pattern, path.split("?", 1)[0]) is not None


class OAuthUpstream(BaseModel):
    """An oauth2 token-minting injection rule, minted by the proxy itself.

    Unlike ``Upstream.inject``, there's no static value to template — the
    proxy has to actively call ``token_url`` on its own, independent of any
    agent-initiated request, then cache and refresh the result. See
    ``_OAuthTokenCache``.

    Supports three grant types, distinguished by ``grant_type`` and which
    of the optional fields below are populated (see
    ``_mint_oauth_token_sync`` for the exact request body each builds):
    ``client_credentials`` (client_id/client_secret only), ``password``
    (adds username/password — the resource owner password credentials
    grant), and ``refresh_token`` (adds refresh_token, exchanged for a
    fresh access token on every mint — there's no interactive flow to
    obtain a new refresh_token, so the one on file is reused as-is each
    time; a provider that rotates refresh tokens on use isn't supported).
    """

    host: str
    header: str = "Authorization"
    path_prefix: str | None = None
    token_url: str
    grant_type: str = "client_credentials"
    scope: str | None = None
    client_id: str | None = None
    client_secret: str | None = None
    username: str | None = None
    password: str | None = None
    refresh_token: str | None = None


# GitLab project-scoped REST calls address the project by its URL-encoded
# full path (e.g. ``/api/v4/projects/my-group%2Fmy-repo/issues/10``), not by
# a literal ``/my-group/my-repo/`` URL path — the encoded segment has to be
# decoded and treated as the path for ``path_prefix`` matching to apply.
# ``flow.request.path`` includes the query string, so the segment must stop
# at ``?`` too, not just the next ``/``.
_GITLAB_API_PROJECT_RE = re.compile(r"^/api/v4/projects/([^/?]+)")

# git's smart-HTTP client tries a repo URL both with and without a trailing
# ``.git`` (e.g. ``/ns/repo/info/refs`` and ``/ns/repo.git/info/refs``) —
# strip it so both forms match the same ``path_prefix``.
_GIT_SUFFIX_RE = re.compile(r"\.git(?=/|$)")


def _gitlab_match_path(path: str) -> str:
    """Normalize a GitLab request path onto the ``/namespace/repo/...`` shape
    ``path_prefix`` values are expressed in, so both git smart-HTTP and REST
    API calls scoped to the same project match the same prefix.

    A numeric project id (``/api/v4/projects/123/...``) carries no namespace
    info and is returned unchanged — it won't match any prefix, which is the
    safe (fail-closed) outcome given we have nothing to route it by.
    """
    api_match = _GITLAB_API_PROJECT_RE.match(path)
    if api_match:
        decoded = unquote(api_match.group(1))
        if not decoded.isdigit():
            return "/" + decoded.strip("/") + "/"
        return path
    return _GIT_SUFFIX_RE.sub("", path)


class Config(BaseModel):
    execution_id: str
    upstreams: list[Upstream] = Field(default_factory=list)
    oauth_upstreams: list[OAuthUpstream] = Field(default_factory=list)

    def hosts(self) -> set[str]:
        """Raw configured host patterns (may include ``*.example.com`` wildcards).

        For introspection/tests, not for allowlist checks — a wildcard
        pattern isn't itself a host, so membership against this set doesn't
        tell you whether a given host is allowed. Use ``allows_host`` for that.
        """
        return {u.host for u in self.upstreams} | {u.host for u in self.oauth_upstreams}

    def allows_host(self, host: str) -> bool:
        """Whether any configured upstream (plain or oauth) covers ``host``."""
        return any(_host_matches(u.host, host) for u in self.upstreams) or any(
            _host_matches(u.host, host) for u in self.oauth_upstreams
        )

    def match(self, host: str, path: str, method: str = "GET") -> Upstream | None:
        """Return the most specific upstream for this host/path.

        Multiple upstreams can share a host, scoped by ``path_prefix``
        (e.g. one GitLab token per namespace on the same host). An exact
        host match beats a ``*.example.com`` wildcard match, and within
        that tier the longest matching path prefix wins; an entry with no
        prefix matches any path on that host as a fallback. A
        ``path_pattern`` rule sits between the two: a real prefix match
        still wins over it, a host-wide fallback doesn't.
        """
        prefixed = [u for u in self.upstreams if u.path_pattern is None]
        best = _longest_prefix_match(prefixed, host, path)
        if best is not None and best.path_prefix is not None:
            return best
        for upstream in self.upstreams:
            if _host_matches(upstream.host, host) and upstream.matches_pattern(
                method, path
            ):
                return upstream
        return best

    def match_oauth(self, host: str, path: str) -> OAuthUpstream | None:
        """Same longest-prefix rule as ``match``, over the oauth2 upstreams."""
        return _longest_prefix_match(self.oauth_upstreams, host, path)

    def body_secrets(self, host: str) -> dict[str, str]:
        """Every body secret bound to ``host``, whichever upstream wins the header match."""
        found: dict[str, str] = {}
        for upstream in self.upstreams:
            if upstream.body and _host_matches(upstream.host, host):
                found.update(upstream.body)
        return found

    def totp_codes(self, host: str) -> dict[str, str]:
        """The current code for every TOTP seed bound to ``host``."""
        return {
            placeholder: pyotp.parse_uri(uri).at(time.time())
            for upstream in self.upstreams
            if upstream.totp and _host_matches(upstream.host, host)
            for placeholder, uri in upstream.totp.items()
        }


def _host_matches(pattern: str, host: str) -> bool:
    """True if ``host`` is covered by ``pattern``.

    ``pattern`` is either an exact hostname or a leading wildcard like
    ``*.example.com``, which covers any subdomain (``api.example.com``) but
    — same convention as a wildcard TLS cert — not the apex (``example.com``)
    itself; register that separately if it also needs to match.
    """
    if pattern.startswith("*."):
        return host.endswith(pattern[1:])
    return pattern == host


def _host_specificity(pattern: str) -> int:
    """Exact hosts outrank wildcard hosts when both match the same request."""
    return 0 if pattern.startswith("*.") else 1


def _longest_prefix_match(candidates: list[Any], host: str, path: str) -> Any | None:
    normalized = _gitlab_match_path(path)
    best: Any | None = None
    best_key: tuple[int, int] | None = None
    for upstream in candidates:
        if not _host_matches(upstream.host, host):
            continue
        if upstream.path_prefix is not None and not normalized.startswith(
            upstream.path_prefix
        ):
            continue
        key = (_host_specificity(upstream.host), len(upstream.path_prefix or ""))
        if best_key is None or key > best_key:
            best = upstream
            best_key = key
    return best


def _validate_host_pattern(host: str) -> None:
    """Reject malformed wildcard patterns at load time (fail fast, not at
    request time as a silent, permanent 403)."""
    if "*" not in host:
        return
    if not host.startswith("*.") or host.count("*") != 1 or len(host) <= 2:
        raise ValueError(
            f"invalid wildcard host pattern: {host!r} (expected '*.example.com')"
        )


def expand_env(template: str, env: dict[str, str]) -> str:
    out: list[str] = []
    i = 0
    while i < len(template):
        ch = template[i]
        if ch != "$":
            out.append(ch)
            i += 1
            continue
        nxt = template[i + 1] if i + 1 < len(template) else ""
        if nxt == "$":
            out.append("$")
            i += 2
        elif nxt == "{":
            end = template.find("}", i + 2)
            if end == -1:
                raise ValueError("unterminated ${...} reference in config")
            name = template[i + 2 : end]
            if name not in env:
                raise KeyError(
                    f"config references env var ${{{name}}} which is not set"
                )
            out.append(env[name])
            i = end + 1
        else:
            out.append("$")
            i += 1
    return "".join(out)


def load_config(path: str | os.PathLike[str], env: dict[str, str]) -> Config:
    raw: dict[str, Any] = json.loads(Path(path).read_text())
    by_key: dict[tuple[str, str | None, str | None, tuple[str, ...]], Upstream] = {}
    for entry in raw.get("upstreams", []):
        host = (entry.get("host") or "").strip().lower()
        if not host:
            raise ValueError("upstream with empty host in config")
        _validate_host_pattern(host)
        path_prefix = entry.get("path_prefix")
        path_pattern = entry.get("path_pattern")
        methods = (
            [m.upper() for m in entry["methods"]] if entry.get("methods") else None
        )
        if path_pattern is not None:
            re.compile(path_pattern)
        key = (host, path_prefix, path_pattern, tuple(methods or ()))
        upstream = by_key.setdefault(
            key,
            Upstream(
                host=host,
                path_prefix=path_prefix,
                path_pattern=path_pattern,
                methods=methods,
            ),
        )
        for name, value_template in (entry.get("inject") or {}).items():
            upstream.inject[name.lower()] = expand_env(value_template, env)
        for placeholder, value_template in (entry.get("body") or {}).items():
            if not placeholder:
                raise ValueError(f"empty body placeholder for {host} in config")
            upstream.body[placeholder] = expand_env(value_template, env)
        for placeholder, value_template in (entry.get("totp") or {}).items():
            uri = expand_env(value_template, env)
            if not placeholder or not isinstance(pyotp.parse_uri(uri), pyotp.TOTP):
                raise ValueError(f"bad TOTP entry for {host} in config")
            upstream.totp[placeholder] = uri

    oauth_upstreams: list[OAuthUpstream] = []
    for entry in raw.get("oauth_upstreams", []):
        host = (entry.get("host") or "").strip().lower()
        if not host:
            raise ValueError("oauth_upstream with empty host in config")
        _validate_host_pattern(host)
        # client_id/client_secret/username/password/refresh_token are ${VAR}
        # templates like Upstream.inject values — never present in the
        # config JSON in the clear. Only client_id/client_secret are
        # required; the other three are grant-type-specific (see
        # OAuthUpstream's docstring).
        oauth_upstreams.append(
            OAuthUpstream(
                host=host,
                header=entry.get("header") or "Authorization",
                path_prefix=entry.get("path_prefix"),
                token_url=entry["token_url"],
                grant_type=entry.get("grant_type") or "client_credentials",
                scope=entry.get("scope"),
                client_id=expand_env(entry["client_id"], env)
                if entry.get("client_id")
                else None,
                client_secret=expand_env(entry["client_secret"], env)
                if entry.get("client_secret")
                else None,
                username=expand_env(entry["username"], env)
                if entry.get("username")
                else None,
                password=expand_env(entry["password"], env)
                if entry.get("password")
                else None,
                refresh_token=expand_env(entry["refresh_token"], env)
                if entry.get("refresh_token")
                else None,
            )
        )

    return Config(
        execution_id=raw["execution_id"],
        upstreams=list(by_key.values()),
        oauth_upstreams=oauth_upstreams,
    )


def _mint_oauth_token_sync(upstream: OAuthUpstream) -> tuple[str, int]:
    """Blocking OAuth2 token exchange — run via ``asyncio.to_thread``.

    Uses stdlib ``urllib`` rather than adding an HTTP client dependency;
    this call happens once per token lifetime (cached), not per request.

    Body shape depends on ``grant_type``: ``password`` adds
    username/password, ``refresh_token`` adds refresh_token in place of
    those; client_id/client_secret ride along on all three whenever the
    credential carries them (some providers require client auth even on
    the password/refresh_token grants, others accept a public client).
    """
    data: dict[str, str] = {"grant_type": upstream.grant_type}
    if upstream.grant_type == "password":
        if not upstream.username or not upstream.password:
            raise ValueError("password grant requires username and password")
        data["username"] = upstream.username
        data["password"] = upstream.password
    elif upstream.grant_type == "refresh_token":
        if not upstream.refresh_token:
            raise ValueError("refresh_token grant requires refresh_token")
        data["refresh_token"] = upstream.refresh_token
    if upstream.client_id:
        data["client_id"] = upstream.client_id
    if upstream.client_secret:
        data["client_secret"] = upstream.client_secret
    if upstream.scope:
        data["scope"] = upstream.scope
    body = urlencode(data).encode("utf-8")
    req = urllib.request.Request(
        upstream.token_url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(
        req, timeout=OAUTH_TOKEN_REQUEST_TIMEOUT_SECONDS
    ) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    token = payload["access_token"]
    expires_in = int(payload.get("expires_in", 3600))
    return token, expires_in


class _OAuthTokenCache:
    """Per-sidecar cache of minted oauth2 access tokens.

    Keyed by ``(token_url, client_id)`` — one sidecar per execution, so a
    process-local dict is enough; no cross-execution sharing is possible
    or desired. An ``asyncio.Lock`` per key prevents two concurrent
    requests for the same upstream from both minting on a cold cache.
    """

    def __init__(self) -> None:
        self._tokens: dict[tuple[str, str], tuple[str, float]] = {}
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}

    def _lock_for(self, key: tuple[str, str]) -> asyncio.Lock:
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

    async def get_token(self, upstream: OAuthUpstream) -> str:
        key = (upstream.token_url, upstream.client_id)
        cached = self._tokens.get(key)
        now = time.monotonic()
        if cached is not None and cached[1] > now:
            return cached[0]

        async with self._lock_for(key):
            # Re-check: another waiter may have minted while we queued for the lock.
            cached = self._tokens.get(key)
            now = time.monotonic()
            if cached is not None and cached[1] > now:
                return cached[0]

            token, expires_in = await asyncio.to_thread(
                _mint_oauth_token_sync, upstream
            )
            ttl = max(expires_in - OAUTH_TOKEN_REFRESH_MARGIN_SECONDS, 5)
            self._tokens[key] = (token, time.monotonic() + ttl)
            return token


def _encode_for_body(value: str, content_type: str) -> str:
    """The secret as it has to appear inside a body of this type, so a ``&``
    or a quote in a password can't break the form or the JSON around it."""
    if content_type.startswith("application/x-www-form-urlencoded"):
        return quote_plus(value)
    if content_type.startswith("application/json") or content_type.endswith("+json"):
        return json.dumps(value)[1:-1]
    return value


def _substitute_body_secrets(flow: http.HTTPFlow, body_secrets: dict[str, str]) -> bool:
    """Swap each placeholder in the request body for its real value; True if any was."""
    request = flow.request
    if not body_secrets or "content-encoding" in request.headers:
        return False
    raw = request.raw_content
    if not raw or len(raw) > MAX_BODY_SUBSTITUTION_BYTES:
        return False
    content_type = request.headers.get("content-type", "").lower()
    replaced = raw
    for placeholder, value in body_secrets.items():
        replaced = replaced.replace(
            placeholder.encode(), _encode_for_body(value, content_type).encode()
        )
    if replaced == raw:
        return False
    request.content = replaced
    return True


class _SecretScrubber:
    """Masks body secrets in a response, so a host that echoes what it was
    sent (a redisplayed login form, a stored profile field, a validation
    error) can't hand the real value back to the agent.

    Masks keep the length, so a streamed body still matches its
    ``Content-Length``. Used as a ``stream`` callable it holds back the tail
    of each chunk, in case a secret straddles two.
    """

    def __init__(self, values: list[str]) -> None:
        forms: set[bytes] = set()
        for value in values:
            for form in (
                value,
                json.dumps(value)[1:-1],
                quote_plus(value),
                quote(value, safe=""),
                html.escape(value),
                html.escape(value, quote=False),
            ):
                if form:
                    forms.add(form.encode())
        self._forms = sorted(forms, key=len, reverse=True)
        self._hold = max((len(f) for f in self._forms), default=1) - 1
        self._pending = b""

    def scrub(self, data: bytes) -> bytes:
        for form in self._forms:
            data = data.replace(form, b"*" * len(form))
        return data

    def scrub_text(self, text: str) -> str:
        return self.scrub(text.encode("utf-8", "surrogateescape")).decode(
            "utf-8", "surrogateescape"
        )

    def __call__(self, chunk: bytes) -> bytes:
        data = self.scrub(self._pending + chunk)
        if not chunk or not self._hold:
            self._pending = b""
            return data
        self._pending = data[-self._hold :]
        return data[: -self._hold]


def _cookie_domain_covers(domain: str, host: str) -> bool:
    return host == domain or host.endswith("." + domain)


class _VaultedCookie(BaseModel):
    value: str
    host: str
    # The cookie's ``Domain`` attribute; ``None`` for a host-only cookie.
    domain: str | None = None

    def sent_to(self, host: str) -> bool:
        if self.domain is None:
            return host == self.host
        return _cookie_domain_covers(self.domain, host)


class _CookieVault:
    """Session cookies the agent only ever sees as placeholders.

    A ``set-cookie`` value is kept here and the browser gets a random
    placeholder with the same attributes, so it still handles domain, path,
    expiry and deletion itself. On the way out the placeholder is swapped
    back, but only for a host the cookie would really be sent to — a
    placeholder pasted into a request to another allowlisted host stays
    worthless.
    """

    def __init__(self) -> None:
        self._cookies: dict[str, _VaultedCookie] = {}

    def vault(self, value: str, host: str, domain: str | None) -> str:
        if len(self._cookies) >= MAX_VAULTED_COOKIES:
            self._cookies.pop(next(iter(self._cookies)))
        placeholder = COOKIE_PLACEHOLDER_PREFIX + secrets.token_urlsafe(24)
        self._cookies[placeholder] = _VaultedCookie(
            value=value, host=host, domain=domain
        )
        return placeholder

    def reveal(self, placeholder: str, host: str) -> str | None:
        cookie = self._cookies.get(placeholder)
        if cookie is None or not cookie.sent_to(host):
            return None
        return cookie.value

    def rewrite_set_cookie(self, header: str, host: str, *, vault_all: bool) -> str:
        """Vault the value of one ``set-cookie`` header.

        Only ``HttpOnly`` cookies unless ``vault_all``: page scripts never
        read those, so a placeholder can't break the page, while a cookie
        JS does read (CSRF double-submit, client flags) has to stay real.
        """
        pair, sep, attrs = header.partition(";")
        name, eq, value = pair.partition("=")
        value = value.strip()
        if not eq or not value:
            return header
        attributes: dict[str, str] = {}
        for attr in attrs.split(";"):
            key, _, attr_value = attr.partition("=")
            attributes[key.strip().lower()] = attr_value.strip()
        if not vault_all and "httponly" not in attributes:
            return header
        domain = attributes.get("domain", "").lstrip(".").lower() or None
        if domain is not None and (
            "." not in domain or not _cookie_domain_covers(domain, host)
        ):
            # The browser rejects such a cookie; never let it widen the reveal.
            domain = None
        placeholder = self.vault(value, host, domain)
        return f"{name.strip()}={placeholder}{sep}{attrs}"

    def reveal_cookie_header(self, header: str, host: str) -> str:
        if COOKIE_PLACEHOLDER_PREFIX not in header:
            return header
        pairs: list[str] = []
        for item in header.split(";"):
            name, eq, value = item.strip().partition("=")
            if eq and value.startswith(COOKIE_PLACEHOLDER_PREFIX):
                value = self.reveal(value, host) or value
            pairs.append(f"{name}{eq}{value}")
        return "; ".join(pairs)


def _audit(
    execution_id: str,
    host: str,
    method: str,
    path: str,
    outcome: str,
    status: int | None,
    injected: list[str] | None = None,
) -> None:
    """``injected`` is the header names this request had credentials written
    into — ``[]`` for a request that matched an allowlist-only upstream and
    went out bare. Without it a credential-less pass-through is
    indistinguishable in the audit trail from an authenticated one, which is
    the difference between "the token is wrong" and "no token was sent".
    """
    event = {
        "ts": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "execution_id": execution_id,
        "host": host,
        "method": method,
        "path": path,
        "outcome": outcome,
        "status": status,
        "injected": injected,
        "bytes_in": None,
        "bytes_out": None,
        "error": None,
    }
    sys.stdout.write(json.dumps(event) + "\n")
    sys.stdout.flush()


class SecurityProxy:
    def __init__(self, config: Config | None = None) -> None:
        self.config = config
        self._oauth_cache = _OAuthTokenCache()
        self._cookie_vault = _CookieVault()

    def running(self) -> None:
        if self.config is None:
            path = os.environ.get("SECURITY_PROXY_CONFIG", DEFAULT_CONFIG_PATH)
            self.config = load_config(path, dict(os.environ))

    def http_connect(self, flow: http.HTTPFlow) -> None:
        assert self.config is not None
        host = flow.request.pretty_host.lower()
        if not self.config.allows_host(host):
            _audit(self.config.execution_id, host, "CONNECT", "", "denied_host", 403)
            flow.response = http.Response.make(
                403,
                b"denied: host not in allowlist\n",
                {"Content-Type": "text/plain; charset=utf-8"},
            )
            return
        _audit(self.config.execution_id, host, "CONNECT", "", "forwarded", None)

    async def request(self, flow: http.HTTPFlow) -> None:
        assert self.config is not None
        host = flow.request.pretty_host.lower()
        method = flow.request.method
        path = flow.request.path

        upstream = self.config.match(host, path, method)
        if upstream is not None:
            for header in STRIPPED_HEADERS:
                if header in flow.request.headers:
                    del flow.request.headers[header]
            for name, value in upstream.inject.items():
                flow.request.headers[name] = value
            injected = sorted(upstream.inject)
            body_secrets = self.config.body_secrets(host)
            if body_secrets:
                # An encoded response can't be scrubbed as it streams.
                if "accept-encoding" in flow.request.headers:
                    del flow.request.headers["accept-encoding"]
                flow.metadata["jc_scrub"] = list(body_secrets.values())
            if _substitute_body_secrets(flow, body_secrets):
                injected.append("body")
            if _substitute_body_secrets(flow, self.config.totp_codes(host)):
                injected.append("totp")
            if self._reveal_cookies(flow, host):
                injected.append("cookie")
            flow.metadata["jc_injected"] = bool(injected)
            _audit(
                self.config.execution_id,
                host,
                method,
                path,
                "forwarded",
                None,
                injected=injected,
            )
            return

        oauth_upstream = self.config.match_oauth(host, path)
        if oauth_upstream is not None:
            try:
                token = await self._oauth_cache.get_token(oauth_upstream)
            except (urllib.error.URLError, TimeoutError, KeyError, ValueError, OSError):
                # Fail closed — an unauthenticated pass-through would be
                # worse than a denied request.
                _audit(
                    self.config.execution_id,
                    host,
                    method,
                    path,
                    "oauth_mint_failed",
                    502,
                )
                flow.response = http.Response.make(
                    502,
                    b"denied: failed to mint oauth2 token\n",
                    {"Content-Type": "text/plain; charset=utf-8"},
                )
                return

            for header in STRIPPED_HEADERS:
                if header in flow.request.headers:
                    del flow.request.headers[header]
            flow.request.headers[oauth_upstream.header] = f"Bearer {token}"
            self._reveal_cookies(flow, host)
            _audit(
                self.config.execution_id,
                host,
                method,
                path,
                "forwarded",
                None,
                injected=[oauth_upstream.header.lower()],
            )
            return

        _audit(self.config.execution_id, host, method, path, "denied_host", 403)
        flow.response = http.Response.make(
            403,
            b"denied: host not in allowlist\n",
            {"Content-Type": "text/plain; charset=utf-8"},
        )

    def _reveal_cookies(self, flow: http.HTTPFlow, host: str) -> bool:
        """Swap vaulted cookies back in; True if any was."""
        # HTTP/2 may split cookies over several headers.
        cookies = flow.request.headers.get_all("cookie")
        if not cookies:
            return False
        revealed = [self._cookie_vault.reveal_cookie_header(c, host) for c in cookies]
        flow.request.headers.set_all("cookie", revealed)
        return revealed != cookies

    def responseheaders(self, flow: http.HTTPFlow) -> None:
        """Fires once response headers arrive, before the body does — the
        only point where credential headers can still be stripped *and*
        streaming can still be enabled (both are no-ops once the body has
        already been buffered, which is what ``response`` would see).

        Every response streams: the proxy never reads a body, and mitmproxy
        otherwise buffers each one whole in the 256MiB sidecar. Clone
        packfiles, repo archives, Node downloads and npm tarballs fetched
        16 at a time all OOM-killed it, or would.

        A response to a request the proxy put a credential into (header,
        body secret, vaulted cookie) has every cookie vaulted, since any of
        them may be a session minted from it; elsewhere only ``HttpOnly``
        ones are, so cookies page scripts read (CSRF double-submit) stay
        real. A flow that never went through ``request`` counts as injected.

        On a host with body secrets, headers and body are scrubbed of them.
        A body the server compressed anyway is buffered and scrubbed in
        ``response`` instead.
        """
        if flow.response is None:
            return
        for header in STRIPPED_RESPONSE_HEADERS:
            if header in flow.response.headers:
                del flow.response.headers[header]
        scrub_values = flow.metadata.get("jc_scrub")
        scrubber = _SecretScrubber(scrub_values) if scrub_values else None
        if scrubber is not None:
            headers = flow.response.headers
            for name in set(headers.keys()):
                headers.set_all(
                    name, [scrubber.scrub_text(v) for v in headers.get_all(name)]
                )
        set_cookies = flow.response.headers.get_all("set-cookie")
        if set_cookies:
            host = flow.request.pretty_host.lower()
            vault_all = flow.metadata.get("jc_injected", True)
            flow.response.headers.set_all(
                "set-cookie",
                [
                    self._cookie_vault.rewrite_set_cookie(c, host, vault_all=vault_all)
                    for c in set_cookies
                ],
            )
        if scrubber is None:
            flow.response.stream = True
        elif "content-encoding" in flow.response.headers:
            flow.metadata["jc_scrub_buffered"] = True
        else:
            flow.response.stream = scrubber

    def response(self, flow: http.HTTPFlow) -> None:
        if flow.response is None or not flow.metadata.get("jc_scrub_buffered"):
            return
        scrubber = _SecretScrubber(flow.metadata["jc_scrub"])
        try:
            content = flow.response.content
        except ValueError:
            # Undecodable, so unscrubbable: fail closed.
            flow.response = http.Response.make(
                502,
                b"denied: response could not be scrubbed of body secrets\n",
                {"Content-Type": "text/plain; charset=utf-8"},
            )
            return
        if content:
            flow.response.content = scrubber.scrub(content)


addons = [SecurityProxy()]

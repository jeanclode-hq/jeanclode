"""Tests for the mitmproxy security sidecar.

Covers the parts that are pure logic — config loading + the policy
applied to a request flow. The mitmproxy hooks themselves are exercised
by constructing a real ``HTTPFlow`` because mitmproxy's testutils ship
that for free.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from mitmproxy.http import HTTPFlow, Request, Response

from proxy import (
    Config,
    OAuthUpstream,
    SecurityProxy,
    Upstream,
    _gitlab_match_path,
    _mint_oauth_token_sync,
    _OAuthTokenCache,
    expand_env,
    load_config,
)


def make_flow(
    url: str, method: str = "GET", headers: dict[str, str] | None = None
) -> HTTPFlow:
    req = Request.make(method, url, b"", headers or {})
    flow = HTTPFlow(client_conn=None, server_conn=None)  # type: ignore[arg-type]
    flow.request = req
    return flow


def make_config() -> Config:
    return Config(
        execution_id="exec_test",
        upstreams=[
            Upstream(
                host="api.anthropic.com",
                inject={"x-api-key": "sk-injected"},
            ),
            Upstream(
                host="api.github.com",
                inject={"authorization": "Bearer ghs-injected"},
            ),
            Upstream(host="raw.example.com"),
        ],
    )


def upstream_for(cfg: Config, host: str) -> Upstream:
    return next(u for u in cfg.upstreams if u.host == host and u.path_prefix is None)


@pytest.mark.parametrize(
    ("template", "expected"),
    [
        ("plain", "plain"),
        ("${A}", "alpha"),
        ("Bearer ${A}", "Bearer alpha"),
        ("${A}-${B}", "alpha-beta"),
        ("$$literal", "$literal"),
        ("$ no brace", "$ no brace"),
    ],
)
def test_expand_env(template: str, expected: str) -> None:
    assert expand_env(template, {"A": "alpha", "B": "beta"}) == expected


def test_expand_env_missing_var_raises() -> None:
    with pytest.raises(KeyError, match="MISSING"):
        expand_env("${MISSING}", {})


def test_expand_env_unterminated_raises() -> None:
    with pytest.raises(ValueError, match="unterminated"):
        expand_env("${UNCLOSED", {})


def test_load_config_resolves_and_merges_duplicate_hosts(tmp_path: Path) -> None:
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(
        json.dumps(
            {
                "execution_id": "exec_1",
                "upstreams": [
                    {"host": "api.anthropic.com", "inject": {"x-api-key": "${ANT}"}},
                    {
                        "host": "API.anthropic.com",
                        "inject": {"Authorization": "Bearer ${CC}"},
                    },
                ],
            }
        )
    )
    cfg = load_config(cfg_file, {"ANT": "sk-ant", "CC": "ccoauth"})

    assert cfg.execution_id == "exec_1"
    assert cfg.hosts() == {"api.anthropic.com"}
    inject = upstream_for(cfg, "api.anthropic.com").inject
    assert inject == {"x-api-key": "sk-ant", "authorization": "Bearer ccoauth"}


def test_load_config_empty_inject_is_allowlist_only(tmp_path: Path) -> None:
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(
        json.dumps(
            {
                "execution_id": "x",
                "upstreams": [{"host": "raw.example.com", "inject": {}}],
            }
        )
    )
    cfg = load_config(cfg_file, {})
    assert upstream_for(cfg, "raw.example.com").inject == {}


def test_load_config_empty_upstreams_denies_everything(tmp_path: Path) -> None:
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps({"execution_id": "x", "upstreams": []}))
    cfg = load_config(cfg_file, {})
    assert cfg.upstreams == []


def test_load_config_keeps_distinct_path_prefixes_on_same_host(tmp_path: Path) -> None:
    """Two GitLab namespaces on one host must stay separate, each keeping its
    own token — this is the exact shape that regresses to token bleed if
    upstreams get merged by host alone. Paths use the real REST API shape
    (URL-encoded namespace, not a literal ``/org/repo/`` URL path)."""
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(
        json.dumps(
            {
                "execution_id": "x",
                "upstreams": [
                    {
                        "host": "gitlab.example.com",
                        "path_prefix": "/jdoe/webshop/",
                        "inject": {"private-token": "${TOKEN_WEBSHOP}"},
                    },
                    {
                        "host": "gitlab.example.com",
                        "path_prefix": "/jdoe/reports-api/",
                        "inject": {"private-token": "${TOKEN_OTHER}"},
                    },
                ],
            }
        )
    )
    cfg = load_config(cfg_file, {"TOKEN_WEBSHOP": "tok-q", "TOKEN_OTHER": "tok-o"})

    assert len(cfg.upstreams) == 2
    webshop = cfg.match(
        "gitlab.example.com", "/api/v4/projects/jdoe%2Fwebshop/issues/10"
    )
    other = cfg.match(
        "gitlab.example.com", "/api/v4/projects/jdoe%2Freports-api/issues/1"
    )
    assert webshop is not None and webshop.inject["private-token"] == "tok-q"
    assert other is not None and other.inject["private-token"] == "tok-o"


def test_config_match_falls_back_to_no_prefix_entry() -> None:
    cfg = Config(
        execution_id="x",
        upstreams=[
            Upstream(host="gitlab.example.com", inject={"private-token": "default"}),
            Upstream(
                host="gitlab.example.com",
                path_prefix="/jdoe/webshop/",
                inject={"private-token": "scoped"},
            ),
        ],
    )
    assert (
        cfg.match(
            "gitlab.example.com", "/api/v4/projects/jdoe%2Fwebshop/issues/10"
        ).inject["private-token"]  # type: ignore[union-attr]
        == "scoped"
    )
    assert (
        cfg.match(
            "gitlab.example.com", "/api/v4/projects/jdoe%2Fother/issues/1"
        ).inject["private-token"]  # type: ignore[union-attr]
        == "default"
    )


def test_config_match_no_matching_prefix_and_no_fallback_returns_none() -> None:
    cfg = Config(
        execution_id="x",
        upstreams=[
            Upstream(
                host="gitlab.example.com",
                path_prefix="/jdoe/webshop/",
                inject={"private-token": "scoped"},
            ),
        ],
    )
    assert (
        cfg.match("gitlab.example.com", "/api/v4/projects/jdoe%2Fother/issues/1")
        is None
    )


def test_config_match_routes_numeric_project_id_via_explicit_prefix() -> None:
    """`glab api -R <repo> "projects/:id/..."` — the standard way to call the
    GitLab REST API for a known repo — resolves `:id` to the project's
    numeric id before issuing the real request, which then carries no
    namespace for a slug prefix like ``/jdoe/webshop/`` to match. The fix is
    on the config side: the backend also emits a numeric-id-keyed prefix
    (``/api/v4/projects/<external_id>/``) per repo, using the repo's own
    token. The proxy itself needs no numeric-id-specific logic — plain
    longest-prefix matching already routes it once that entry exists. This
    is the exact production break: ``POST /api/v4/projects/252/merge_requests/
    760/notes`` was denied while the equivalent slug-addressed call worked."""
    cfg = Config(
        execution_id="x",
        upstreams=[
            Upstream(
                host="gitlab.example.com",
                path_prefix="/jdoe/webshop/",
                inject={"private-token": "tok-webshop"},
            ),
            Upstream(
                host="gitlab.example.com",
                path_prefix="/api/v4/projects/252/",
                inject={"private-token": "tok-webshop"},
            ),
            Upstream(
                host="gitlab.example.com",
                path_prefix="/jdoe/reports-api/",
                inject={"private-token": "tok-sending"},
            ),
            Upstream(
                host="gitlab.example.com",
                path_prefix="/api/v4/projects/999/",
                inject={"private-token": "tok-sending"},
            ),
        ],
    )

    by_slug = cfg.match(
        "gitlab.example.com", "/api/v4/projects/jdoe%2Fwebshop/issues/10"
    )
    by_id = cfg.match(
        "gitlab.example.com", "/api/v4/projects/252/merge_requests/760/notes"
    )
    assert by_slug is not None and by_id is not None
    assert (
        by_slug.inject["private-token"]
        == by_id.inject["private-token"]
        == "tok-webshop"
    )

    # A numeric id belonging to the *other* repo must not cross-match.
    other_by_id = cfg.match("gitlab.example.com", "/api/v4/projects/999/issues/1")
    assert (
        other_by_id is not None and other_by_id.inject["private-token"] == "tok-sending"
    )


def test_config_match_numeric_project_id_without_configured_prefix_stays_denied() -> (
    None
):
    """Fail-closed default preserved: an id the backend never emitted a
    prefix for (e.g. a repo it hasn't synced yet) stays unroutable — this
    isn't a proxy-side allowance, it only works for ids the backend chose
    to authorize explicitly."""
    cfg = Config(
        execution_id="x",
        upstreams=[
            Upstream(
                host="gitlab.example.com",
                path_prefix="/jdoe/webshop/",
                inject={"private-token": "tok-webshop"},
            ),
            Upstream(
                host="gitlab.example.com",
                path_prefix="/api/v4/projects/252/",
                inject={"private-token": "tok-webshop"},
            ),
        ],
    )
    assert cfg.match("gitlab.example.com", "/api/v4/projects/999/issues/1") is None


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        # git smart-HTTP: both forms the git client tries must resolve to
        # the same namespace path — this is the exact regression that broke
        # `git clone` in production (only the .git-suffixed form was denied).
        # flow.request.path includes the query string, as captured in the
        # real audit log from the broken run.
        (
            "/jdoe/webshop/info/refs?service=git-upload-pack",
            "/jdoe/webshop/info/refs?service=git-upload-pack",
        ),
        (
            "/jdoe/webshop.git/info/refs?service=git-upload-pack",
            "/jdoe/webshop/info/refs?service=git-upload-pack",
        ),
        ("/jdoe/webshop.git/git-upload-pack", "/jdoe/webshop/git-upload-pack"),
        # REST API: URL-encoded full project path must decode to the literal
        # namespace path our path_prefix values are expressed in.
        ("/api/v4/projects/jdoe%2Fwebshop/issues/10", "/jdoe/webshop/"),
        ("/api/v4/projects/jdoe%2Fwebshop/issues/10/notes", "/jdoe/webshop/"),
        # nested subgroup
        ("/api/v4/projects/jdoe%2Fsub%2Fwebshop/issues/10", "/jdoe/sub/webshop/"),
        # project segment directly followed by a query string, no further path
        ("/api/v4/projects/jdoe%2Fwebshop?simple=1", "/jdoe/webshop/"),
        # numeric project id carries no namespace — passed through unchanged,
        # so it deliberately matches nothing (fail closed, not fail open).
        (
            "/api/v4/projects/250/merge_requests/633/notes",
            "/api/v4/projects/250/merge_requests/633/notes",
        ),
    ],
)
def test_gitlab_match_path_normalizes_real_request_shapes(
    path: str, expected: str
) -> None:
    assert _gitlab_match_path(path) == expected


def test_load_config_empty_host_rejected(tmp_path: Path) -> None:
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(
        json.dumps({"execution_id": "x", "upstreams": [{"host": "", "inject": {}}]})
    )
    with pytest.raises(ValueError, match="empty host"):
        load_config(cfg_file, {})


@pytest.mark.parametrize("host", ["*", "*.", "api.*.com", "*.a.*.com"])
def test_load_config_malformed_wildcard_host_rejected(
    tmp_path: Path, host: str
) -> None:
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(
        json.dumps({"execution_id": "x", "upstreams": [{"host": host, "inject": {}}]})
    )
    with pytest.raises(ValueError, match="invalid wildcard host pattern"):
        load_config(cfg_file, {})


def test_config_match_wildcard_host_covers_subdomain() -> None:
    """A skill/connector host registered as `*.figma.com` must cover the
    actual wire hosts a real API is split across (`api.figma.com`,
    `www.figma.com`), not just the literal pattern string."""
    cfg = Config(
        execution_id="x",
        upstreams=[Upstream(host="*.figma.com", inject={"x-figma-token": "tok"})],
    )
    assert cfg.match("api.figma.com", "/v1/files/abc").inject["x-figma-token"] == "tok"  # type: ignore[union-attr]
    assert cfg.match("www.figma.com", "/anything").inject["x-figma-token"] == "tok"  # type: ignore[union-attr]


def test_config_match_wildcard_host_excludes_apex() -> None:
    """`*.figma.com` covers subdomains only — same convention as a wildcard
    TLS cert. A bare `figma.com` request needs its own separate entry."""
    cfg = Config(
        execution_id="x",
        upstreams=[Upstream(host="*.figma.com", inject={"x-figma-token": "tok"})],
    )
    assert cfg.match("figma.com", "/anything") is None


def test_config_match_prefers_exact_host_over_wildcard() -> None:
    """When both an exact host and a covering wildcard are configured, the
    more specific exact entry wins, regardless of list order."""
    cfg = Config(
        execution_id="x",
        upstreams=[
            Upstream(host="*.figma.com", inject={"x-figma-token": "wildcard"}),
            Upstream(host="api.figma.com", inject={"x-figma-token": "exact"}),
        ],
    )
    assert (
        cfg.match("api.figma.com", "/v1/files/abc").inject["x-figma-token"] == "exact"
    )  # type: ignore[union-attr]
    assert (
        cfg.match("www.figma.com", "/v1/files/abc").inject["x-figma-token"]
        == "wildcard"
    )  # type: ignore[union-attr]


def test_request_wildcard_host_injects_credential() -> None:
    proxy = SecurityProxy(
        Config(
            execution_id="x",
            upstreams=[Upstream(host="*.figma.com", inject={"x-figma-token": "tok"})],
        )
    )
    flow = make_flow("https://api.figma.com/v1/files/abc")
    asyncio.run(proxy.request(flow))
    assert flow.response is None
    assert flow.request.headers["x-figma-token"] == "tok"


def test_http_connect_allows_host_covered_by_wildcard() -> None:
    proxy = SecurityProxy(
        Config(
            execution_id="x",
            upstreams=[Upstream(host="*.figma.com", inject={})],
        )
    )
    flow = make_flow("https://api.figma.com:443", method="CONNECT")
    proxy.http_connect(flow)
    assert flow.response is None


def test_http_connect_denies_apex_not_covered_by_wildcard() -> None:
    proxy = SecurityProxy(
        Config(
            execution_id="x",
            upstreams=[Upstream(host="*.figma.com", inject={})],
        )
    )
    flow = make_flow("https://figma.com:443", method="CONNECT")
    proxy.http_connect(flow)
    assert flow.response is not None
    assert flow.response.status_code == 403


def test_request_forwards_known_host_and_injects() -> None:
    proxy = SecurityProxy(make_config())
    flow = make_flow("https://api.anthropic.com/v1/messages")
    asyncio.run(proxy.request(flow))
    assert flow.response is None  # not short-circuited
    assert flow.request.headers["x-api-key"] == "sk-injected"


def _two_namespace_gitlab_config() -> Config:
    return Config(
        execution_id="exec_test",
        upstreams=[
            Upstream(
                host="gitlab.example.com",
                path_prefix="/jdoe/webshop/",
                inject={"private-token": "tok-webshop"},
            ),
            Upstream(
                host="gitlab.example.com",
                path_prefix="/jdoe/reports-api/",
                inject={"private-token": "tok-sending"},
            ),
        ],
    )


def test_request_routes_by_path_prefix_on_shared_host() -> None:
    """Regression test: two GitLab namespaces sharing a host must each get
    their own token injected based on request path, not whichever entry
    happens to load last for that host. Uses the real REST API URL shape
    (URL-encoded namespace), not a literal ``/org/repo/`` path."""
    proxy = SecurityProxy(_two_namespace_gitlab_config())

    webshop_flow = make_flow(
        "https://gitlab.example.com/api/v4/projects/jdoe%2Fwebshop/issues/10"
    )
    asyncio.run(proxy.request(webshop_flow))
    assert webshop_flow.response is None
    assert webshop_flow.request.headers["private-token"] == "tok-webshop"

    sending_flow = make_flow(
        "https://gitlab.example.com/api/v4/projects/jdoe%2Freports-api/issues/1"
    )
    asyncio.run(proxy.request(sending_flow))
    assert sending_flow.response is None
    assert sending_flow.request.headers["private-token"] == "tok-sending"


def _two_namespace_gitlab_config_with_numeric_ids() -> Config:
    """Same two GitLab namespaces as ``_two_namespace_gitlab_config``, plus
    the numeric-id-keyed prefix the backend now emits alongside each repo's
    slug-keyed one (see ``add_gitlab_workspace_credentials`` in
    ``dispatch_inputs.py``)."""
    return Config(
        execution_id="exec_test",
        upstreams=[
            Upstream(
                host="gitlab.example.com",
                path_prefix="/jdoe/webshop/",
                inject={"private-token": "tok-webshop"},
            ),
            Upstream(
                host="gitlab.example.com",
                path_prefix="/api/v4/projects/252/",
                inject={"private-token": "tok-webshop"},
            ),
            Upstream(
                host="gitlab.example.com",
                path_prefix="/jdoe/reports-api/",
                inject={"private-token": "tok-sending"},
            ),
            Upstream(
                host="gitlab.example.com",
                path_prefix="/api/v4/projects/999/",
                inject={"private-token": "tok-sending"},
            ),
        ],
    )


def test_request_numeric_project_id_routes_to_backend_emitted_prefix() -> None:
    """Regression test for the exact production break: a request addressed
    by numeric project id (what `glab api -R <repo> "projects/:id/..."`
    actually sends on the wire, after resolving `:id` itself) must inject
    the same repo token as the equivalent slug-addressed request — using
    only the config shape the backend now emits, no proxy code change.
    Real failing call from prod: ``POST /api/v4/projects/252/merge_requests/
    760/notes``."""
    proxy = SecurityProxy(_two_namespace_gitlab_config_with_numeric_ids())

    numeric_flow = make_flow(
        "https://gitlab.example.com/api/v4/projects/252/merge_requests/760/notes",
        method="POST",
    )
    asyncio.run(proxy.request(numeric_flow))
    assert numeric_flow.response is None
    assert numeric_flow.request.headers["private-token"] == "tok-webshop"

    # A numeric id from a *different* repo's prefix must not cross-route.
    other_numeric_flow = make_flow(
        "https://gitlab.example.com/api/v4/projects/999/issues/1"
    )
    asyncio.run(proxy.request(other_numeric_flow))
    assert other_numeric_flow.response is None
    assert other_numeric_flow.request.headers["private-token"] == "tok-sending"


def test_request_numeric_project_id_without_configured_prefix_is_denied() -> None:
    """An id the backend never emitted a prefix for (e.g. an unsynced repo)
    still gets a bare 403 from the proxy — the fail-closed default is
    unchanged; only ids the backend explicitly authorized route through."""
    proxy = SecurityProxy(_two_namespace_gitlab_config_with_numeric_ids())

    flow = make_flow("https://gitlab.example.com/api/v4/projects/404/issues/1")
    asyncio.run(proxy.request(flow))
    assert flow.response is not None
    assert flow.response.status_code == 403


def test_request_git_clone_with_dot_git_suffix_is_not_denied() -> None:
    """Regression test for the exact production break: git's smart-HTTP
    client retries the clone URL with a ``.git`` suffix, and that retry must
    still route to the namespace's own token instead of getting a bare 403
    from the proxy itself."""
    proxy = SecurityProxy(_two_namespace_gitlab_config())

    plain_flow = make_flow(
        "https://gitlab.example.com/jdoe/webshop/info/refs?service=git-upload-pack"
    )
    asyncio.run(proxy.request(plain_flow))
    assert plain_flow.response is None
    assert plain_flow.request.headers["private-token"] == "tok-webshop"

    dot_git_flow = make_flow(
        "https://gitlab.example.com/jdoe/webshop.git/info/refs?service=git-upload-pack"
    )
    asyncio.run(proxy.request(dot_git_flow))
    assert dot_git_flow.response is None
    assert dot_git_flow.request.headers["private-token"] == "tok-webshop"


def test_request_strips_caller_supplied_auth_before_injecting() -> None:
    proxy = SecurityProxy(make_config())
    flow = make_flow(
        "https://api.github.com/repos/foo/bar",
        headers={"authorization": "Bearer attacker", "x-api-key": "leak-me"},
    )
    asyncio.run(proxy.request(flow))
    assert flow.response is None
    assert flow.request.headers["authorization"] == "Bearer ghs-injected"
    assert "x-api-key" not in flow.request.headers


@pytest.mark.parametrize(
    "header",
    [
        "authorization",
        "Authorization",
        "proxy-authorization",
        "anthropic-api-key",
        "x-api-key",
    ],
)
def test_request_strips_listed_headers_case_insensitively(header: str) -> None:
    proxy = SecurityProxy(make_config())
    flow = make_flow(
        "https://api.anthropic.com/v1/messages",
        headers={header: "agent-set"},
    )
    asyncio.run(proxy.request(flow))
    assert flow.request.headers["x-api-key"] == "sk-injected"
    assert "authorization" not in flow.request.headers


def test_request_denies_unknown_host_with_403() -> None:
    proxy = SecurityProxy(make_config())
    flow = make_flow("https://attacker.example.com/exfil")
    asyncio.run(proxy.request(flow))
    assert flow.response is not None
    assert flow.response.status_code == 403
    assert b"denied" in flow.response.content


def test_http_connect_denies_unknown_host_with_403() -> None:
    proxy = SecurityProxy(make_config())
    flow = make_flow("https://attacker.example.com:443", method="CONNECT")
    proxy.http_connect(flow)
    assert flow.response is not None
    assert flow.response.status_code == 403


def test_http_connect_allows_known_host() -> None:
    proxy = SecurityProxy(make_config())
    flow = make_flow("https://api.anthropic.com:443", method="CONNECT")
    proxy.http_connect(flow)
    assert flow.response is None


def test_allowlist_only_host_strips_headers_and_forwards() -> None:
    proxy = SecurityProxy(make_config())
    flow = make_flow(
        "https://raw.example.com/some/asset",
        headers={"authorization": "Bearer leaked"},
    )
    asyncio.run(proxy.request(flow))
    assert flow.response is None
    assert "authorization" not in flow.request.headers


@pytest.mark.parametrize(
    "header",
    ["Authorization", "X-Api-Key", "Anthropic-Api-Key", "Private-Token", "Set-Cookie"],
)
def test_response_strips_credential_headers_from_upstream(header: str) -> None:
    """Defense-in-depth — a buggy upstream echoing creds in a response header
    must not reach the agent."""
    proxy = SecurityProxy(make_config())
    flow = make_flow("https://api.github.com/repos/foo/bar")
    flow.response = Response.make(200, b"{}", {header: "leaked-value"})
    proxy.response(flow)
    assert header not in flow.response.headers


def test_response_preserves_normal_headers() -> None:
    proxy = SecurityProxy(make_config())
    flow = make_flow("https://api.github.com/repos/foo/bar")
    flow.response = Response.make(
        200,
        b"{}",
        {"Content-Type": "application/json", "Authorization": "leaked"},
    )
    proxy.response(flow)
    assert flow.response.headers["Content-Type"] == "application/json"
    assert "Authorization" not in flow.response.headers


# ---------------------------------------------------------------------------
# oauth2 client_credentials — proxy-minted tokens
# ---------------------------------------------------------------------------


def test_load_config_expands_oauth_client_credentials(tmp_path: Path) -> None:
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(
        json.dumps(
            {
                "execution_id": "x",
                "upstreams": [],
                "oauth_upstreams": [
                    {
                        "host": "api.example.com",
                        "token_url": "https://auth.example.com/token",
                        "client_id": "${CID}",
                        "client_secret": "${CSEC}",
                    }
                ],
            }
        )
    )
    cfg = load_config(cfg_file, {"CID": "my-id", "CSEC": "my-secret"})
    assert cfg.hosts() == {"api.example.com"}
    upstream = cfg.match_oauth("api.example.com", "/anything")
    assert upstream is not None
    assert upstream.client_id == "my-id"
    assert upstream.client_secret == "my-secret"
    assert upstream.header == "Authorization"
    assert upstream.grant_type == "client_credentials"


def _oauth_config() -> Config:
    return Config(
        execution_id="exec_test",
        oauth_upstreams=[
            OAuthUpstream(
                host="oauth-api.example.com",
                token_url="https://auth.example.com/token",
                client_id="my-id",
                client_secret="my-secret",
            )
        ],
    )


def test_request_mints_and_injects_oauth_token() -> None:
    proxy = SecurityProxy(_oauth_config())
    flow = make_flow("https://oauth-api.example.com/v1/widgets")

    with patch(
        "proxy._mint_oauth_token_sync", return_value=("minted-token", 3600)
    ) as mint:
        asyncio.run(proxy.request(flow))

    assert flow.response is None
    assert flow.request.headers["authorization"] == "Bearer minted-token"
    mint.assert_called_once()


def test_request_reuses_cached_oauth_token() -> None:
    proxy = SecurityProxy(_oauth_config())

    with patch(
        "proxy._mint_oauth_token_sync", return_value=("minted-token", 3600)
    ) as mint:
        asyncio.run(proxy.request(make_flow("https://oauth-api.example.com/a")))
        asyncio.run(proxy.request(make_flow("https://oauth-api.example.com/b")))

    assert mint.call_count == 1


def test_audit_records_which_headers_were_injected(capsys: Any) -> None:
    """A host allowlisted with no injection rule still forwards — so the audit
    line has to say whether anything was actually added, or a credential-less
    pass-through reads exactly like an authenticated one."""
    config = Config(
        execution_id="exec-1",
        upstreams=[
            Upstream(host="git.example.com", path_prefix="/ns/repo/", inject={"authorization": "Basic x"}),
            Upstream(host="git.example.com"),
        ],
    )
    proxy = SecurityProxy(config)

    asyncio.run(proxy.request(make_flow("https://git.example.com/ns/repo/info/refs")))
    asyncio.run(proxy.request(make_flow("https://git.example.com/other/repo/info/refs")))

    events = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert [e["injected"] for e in events] == [["authorization"], []]
    assert {e["outcome"] for e in events} == {"forwarded"}


def test_request_denies_with_502_when_oauth_mint_fails() -> None:
    proxy = SecurityProxy(_oauth_config())
    flow = make_flow("https://oauth-api.example.com/v1/widgets")

    with patch("proxy._mint_oauth_token_sync", side_effect=ValueError("boom")):
        asyncio.run(proxy.request(flow))

    assert flow.response is not None
    assert flow.response.status_code == 502


def test_request_strips_caller_supplied_auth_before_oauth_injection() -> None:
    proxy = SecurityProxy(_oauth_config())
    flow = make_flow(
        "https://oauth-api.example.com/v1/widgets",
        headers={"authorization": "Bearer attacker-supplied"},
    )
    with patch("proxy._mint_oauth_token_sync", return_value=("minted-token", 3600)):
        asyncio.run(proxy.request(flow))

    assert flow.request.headers["authorization"] == "Bearer minted-token"


def test_oauth_token_cache_serializes_concurrent_mints_for_same_key() -> None:
    """Two concurrent callers for the same (token_url, client_id) must
    trigger exactly one mint — the second waits on the lock and reuses the
    first's result rather than minting again."""
    upstream = OAuthUpstream(
        host="h",
        token_url="https://auth.example.com/token",
        client_id="cid",
        client_secret="csec",
    )
    calls = 0

    # patch() replaces the coroutine function ``to_thread`` with an AsyncMock,
    # which awaits a side effect only when the side effect is itself a
    # coroutine function — a lambda handing back a coroutine is returned
    # unawaited, and the caller gets a coroutine where it expects a tuple.
    async def fake_to_thread(_fn: object, _upstream: OAuthUpstream) -> tuple[str, int]:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.01)
        return ("tok", 3600)

    cache = _OAuthTokenCache()

    async def run() -> list[str]:
        with patch("proxy.asyncio.to_thread", side_effect=fake_to_thread):
            return await asyncio.gather(*(cache.get_token(upstream) for _ in range(5)))

    results = asyncio.run(run())
    assert results == ["tok"] * 5
    assert calls == 1


def test_load_config_expands_oauth_password_and_refresh_token_upstreams(
    tmp_path: Path,
) -> None:
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(
        json.dumps(
            {
                "execution_id": "x",
                "upstreams": [],
                "oauth_upstreams": [
                    {
                        "host": "pw.example.com",
                        "token_url": "https://auth.example.com/token",
                        "grant_type": "password",
                        "client_id": "${CID}",
                        "client_secret": "${CSEC}",
                        "username": "${UNAME}",
                        "password": "${UPASS}",
                    },
                    {
                        "host": "rt.example.com",
                        "token_url": "https://auth2.example.com/token",
                        "grant_type": "refresh_token",
                        "client_id": "${CID}",
                        "client_secret": "${CSEC}",
                        "refresh_token": "${RTOK}",
                    },
                ],
            }
        )
    )
    cfg = load_config(
        cfg_file,
        {"CID": "cid", "CSEC": "csec", "UNAME": "bob", "UPASS": "pw", "RTOK": "rt-1"},
    )
    pw_upstream = cfg.match_oauth("pw.example.com", "/x")
    assert pw_upstream is not None
    assert pw_upstream.grant_type == "password"
    assert pw_upstream.username == "bob"
    assert pw_upstream.password == "pw"

    rt_upstream = cfg.match_oauth("rt.example.com", "/x")
    assert rt_upstream is not None
    assert rt_upstream.grant_type == "refresh_token"
    assert rt_upstream.refresh_token == "rt-1"


def test_mint_oauth_token_sync_builds_password_grant_body() -> None:
    upstream = OAuthUpstream(
        host="h",
        token_url="https://auth.example.com/token",
        grant_type="password",
        client_id="cid",
        client_secret="csec",
        username="bob",
        password="pw",
    )
    captured: dict[str, str] = {}

    class FakeResp:
        def __enter__(self) -> "FakeResp":
            return self

        def __exit__(self, *_a: object) -> bool:
            return False

        def read(self) -> bytes:
            return json.dumps({"access_token": "tok", "expires_in": 3600}).encode()

    def fake_urlopen(req: Any, timeout: int | None = None) -> FakeResp:
        captured.update(dict(pair.split("=") for pair in req.data.decode().split("&")))
        return FakeResp()

    with patch("proxy.urllib.request.urlopen", side_effect=fake_urlopen):
        token, expires_in = _mint_oauth_token_sync(upstream)

    assert token == "tok"
    assert expires_in == 3600
    assert captured["grant_type"] == "password"
    assert captured["username"] == "bob"
    assert captured["password"] == "pw"
    assert captured["client_id"] == "cid"


def test_mint_oauth_token_sync_builds_refresh_token_grant_body() -> None:
    upstream = OAuthUpstream(
        host="h",
        token_url="https://auth.example.com/token",
        grant_type="refresh_token",
        client_id="cid",
        client_secret="csec",
        refresh_token="rt-1",
    )
    captured: dict[str, str] = {}

    class FakeResp:
        def __enter__(self) -> "FakeResp":
            return self

        def __exit__(self, *_a: object) -> bool:
            return False

        def read(self) -> bytes:
            return json.dumps({"access_token": "tok", "expires_in": 3600}).encode()

    def fake_urlopen(req: Any, timeout: int | None = None) -> FakeResp:
        captured.update(dict(pair.split("=") for pair in req.data.decode().split("&")))
        return FakeResp()

    with patch("proxy.urllib.request.urlopen", side_effect=fake_urlopen):
        _mint_oauth_token_sync(upstream)

    assert captured["grant_type"] == "refresh_token"
    assert captured["refresh_token"] == "rt-1"
    assert "username" not in captured


def test_mint_oauth_token_sync_password_grant_requires_username_and_password() -> None:
    upstream = OAuthUpstream(
        host="h",
        token_url="https://auth.example.com/token",
        grant_type="password",
        client_id="cid",
        client_secret="csec",
    )
    with pytest.raises(ValueError, match="password grant requires"):
        _mint_oauth_token_sync(upstream)


def test_mint_oauth_token_sync_refresh_token_grant_requires_refresh_token() -> None:
    upstream = OAuthUpstream(
        host="h",
        token_url="https://auth.example.com/token",
        grant_type="refresh_token",
        client_id="cid",
        client_secret="csec",
    )
    with pytest.raises(ValueError, match="refresh_token grant requires"):
        _mint_oauth_token_sync(upstream)


def test_oauth_token_cache_different_keys_mint_independently() -> None:
    upstream_a = OAuthUpstream(
        host="a",
        token_url="https://auth.example.com/token",
        client_id="a",
        client_secret="s",
    )
    upstream_b = OAuthUpstream(
        host="b",
        token_url="https://auth.example.com/token",
        client_id="b",
        client_secret="s",
    )
    cache = _OAuthTokenCache()

    with patch(
        "proxy._mint_oauth_token_sync",
        side_effect=lambda u: (f"tok-{u.client_id}", 3600),
    ):
        tok_a = asyncio.run(cache.get_token(upstream_a))
        tok_b = asyncio.run(cache.get_token(upstream_b))

    assert tok_a == "tok-a"
    assert tok_b == "tok-b"

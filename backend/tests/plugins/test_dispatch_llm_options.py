"""Tests for exposing the LLM credential pool as fixer options (#43)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

from api.database.llm_credentials import LLMCredentialAvailability
from api.plugins.container.dispatch_inputs import DispatchInputs, add_llm_to_inputs
from api.plugins.container.security_proxy import CredentialKey

_MOD = "api.plugins.container.dispatch_inputs"


def _cred(provider: str = "claude_code", **fields: object) -> SimpleNamespace:
    base = {
        "id": uuid4(),
        "provider": provider,
        "name": "",
        "model_high": "sonnet",
        "model_heavy": "",
        "model_low": "haiku",
        "base_url": None,
        "status": "active",
        "stale_until": None,
        "secret_encrypted": f"enc-{provider}",
    }
    base.update(fields)
    return SimpleNamespace(**base)


def _dispatch(pool: list[SimpleNamespace]) -> DispatchInputs:
    app = MagicMock()
    app.options.claude_code.oauth_token = None
    app.options.claude_code.api_key = None
    app.database.session.return_value.__enter__ = MagicMock(return_value=MagicMock())
    app.database.session.return_value.__exit__ = MagicMock(return_value=False)
    inputs = DispatchInputs()
    with (
        patch(f"{_MOD}.get_current_app", return_value=app),
        patch(
            f"{_MOD}.db_select_llm_credential",
            return_value=(LLMCredentialAvailability.AVAILABLE, pool[0], None),
        ),
        patch(f"{_MOD}.db_list_llm_credentials", return_value=pool),
        patch(f"{_MOD}.decrypt_secret", side_effect=lambda c: f"secret-{c.id}"),
    ):
        result = add_llm_to_inputs(inputs)
    assert result.available
    return inputs


def _options(inputs: DispatchInputs) -> list[dict]:
    return json.loads(inputs.public_env["JEANCLODE_LLM_OPTIONS"])


def test_single_credential_without_heavy_model_emits_no_options() -> None:
    inputs = _dispatch([_cred()])
    assert "JEANCLODE_LLM_OPTIONS" not in inputs.public_env
    assert inputs.secrets.keys() == {CredentialKey.CLAUDE_CODE_OAUTH_TOKEN}


def test_primary_heavy_model_alone_emits_options() -> None:
    primary = _cred(model_heavy="opus")
    options = _options(_dispatch([primary]))
    assert options == [
        {
            "id": str(primary.id),
            "name": "claude_code",
            "provider": "claude_code",
            "model_high": "sonnet",
            "model_heavy": "opus",
            "model_low": "haiku",
            "env": {},
            "secret_env": {},
        }
    ]


def test_primary_wiring_is_unchanged_by_extra_options() -> None:
    primary = _cred()
    alone = _dispatch([primary])
    with_extra = _dispatch(
        [primary, _cred("openai_compatible", base_url="https://llm.internal/v1")]
    )
    for key, value in alone.public_env.items():
        assert with_extra.public_env[key] == value
    assert (
        with_extra.secrets[CredentialKey.CLAUDE_CODE_OAUTH_TOKEN]
        == (alone.secrets[CredentialKey.CLAUDE_CODE_OAUTH_TOKEN])
    )
    assert with_extra.public_env["JEANCLODE_LLM_CREDENTIAL_ID"] == str(primary.id)


def test_same_host_credentials_collapse_to_the_primary() -> None:
    inputs = _dispatch([_cred(), _cred(), _cred("anthropic")])
    assert "JEANCLODE_LLM_OPTIONS" not in inputs.public_env
    assert [u.host for u in inputs.upstreams] == ["api.anthropic.com"]


def test_openai_compatible_extra_gets_its_own_secret_and_upstream() -> None:
    extra = _cred(
        "openai_compatible",
        name="self-hosted",
        base_url="https://llm.internal/v1",
        model_high="qwen-coder",
    )
    inputs = _dispatch([_cred(), extra])
    options = _options(inputs)

    assert [o["name"] for o in options] == ["claude_code", "self-hosted"]
    option = options[1]
    secret_key = option["secret_env"]["ANTHROPIC_AUTH_TOKEN"]
    assert inputs.secrets[secret_key] == f"secret-{extra.id}"
    assert option["env"]["ANTHROPIC_BASE_URL"] == "https://llm.internal/v1"
    assert option["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == ""
    upstream = next(u for u in inputs.upstreams if u.secret_key == secret_key)
    assert (upstream.host, upstream.header, upstream.bearer) == (
        "llm.internal",
        "Authorization",
        True,
    )
    assert f"secret-{extra.id}" not in inputs.public_env["JEANCLODE_LLM_OPTIONS"]


def test_claude_extra_behind_openai_primary_resets_the_base_url() -> None:
    primary = _cred("openai", base_url="https://api.openai.com/v1", model_high="gpt-5")
    inputs = _dispatch([primary, _cred(model_heavy="opus")])
    option = _options(inputs)[1]
    assert option["env"]["ANTHROPIC_BASE_URL"] == "https://api.anthropic.com"
    assert option["env"]["ANTHROPIC_AUTH_TOKEN"] == ""
    assert "CLAUDE_CODE_OAUTH_TOKEN" in option["secret_env"]
    assert option["model_heavy"] == "opus"


def test_stale_extra_is_left_out() -> None:
    stale = _cred(
        "openai_compatible",
        base_url="https://llm.internal",
        status="stale",
        stale_until=datetime.now(UTC) + timedelta(hours=1),
    )
    inputs = _dispatch([_cred(), stale])
    assert "JEANCLODE_LLM_OPTIONS" not in inputs.public_env


def test_options_failure_keeps_the_primary() -> None:
    app = MagicMock()
    app.options.claude_code.oauth_token = None
    app.options.claude_code.api_key = None
    primary = _cred()
    inputs = DispatchInputs()
    with (
        patch(f"{_MOD}.get_current_app", return_value=app),
        patch(
            f"{_MOD}.db_select_llm_credential",
            return_value=(LLMCredentialAvailability.AVAILABLE, primary, None),
        ),
        patch(f"{_MOD}.db_list_llm_credentials", side_effect=RuntimeError("boom")),
        patch(f"{_MOD}.decrypt_secret", return_value="tok"),
    ):
        result = add_llm_to_inputs(inputs)
    assert result.available
    assert inputs.secrets[CredentialKey.CLAUDE_CODE_OAUTH_TOKEN] == "tok"
    assert "JEANCLODE_LLM_OPTIONS" not in inputs.public_env

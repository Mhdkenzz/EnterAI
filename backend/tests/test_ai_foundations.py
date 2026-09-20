import json
from unittest.mock import MagicMock, patch

from app.ai.config import AISettings
from app.ai.errors import (
    AIError,
    DuplicateCallDetectedError,
    LoopBudgetExceededError,
    ProviderAuthError,
    ProviderRateLimitedError,
    ProviderTimeoutError,
    ProviderUnavailableError,
    ToolCallValidationError,
)
from app.ai.profiles import DEFAULT_PROFILE, resolve_profile
from app.ai.providers import AnthropicCopilotProvider, OpenAICompatibleCopilotProvider
from app.ai.tool_schema import render_anthropic, render_openai
from app.ai.turn import ProviderTurn, TokenUsage
from app.copilot import TOOL_SPECS, anthropic_tool_specs, openai_tool_specs


def test_provider_turn_defaults_are_backward_compatible():
    # Every existing construction site (both provider classes, both test files)
    # only ever passes text= and tool_calls= -- the new fields must default to
    # something falsy so none of that code needs to change.
    turn = ProviderTurn(text="hello", tool_calls=[{"id": "1", "name": "get_projects", "args": {}}])
    assert turn.finish_reason is None
    assert turn.usage is None
    assert turn.reasoning_text is None
    assert turn.adapter_used is None

    turn_no_calls = ProviderTurn(text="hi")
    assert turn_no_calls.tool_calls == []


def test_token_usage_defaults_to_zero():
    usage = TokenUsage()
    assert usage.prompt_tokens == 0
    assert usage.completion_tokens == 0
    assert usage.reasoning_tokens == 0


def test_ai_error_taxonomy_retryable_flags():
    retryable = {ProviderUnavailableError, ProviderTimeoutError, ProviderRateLimitedError}
    not_retryable = {
        ProviderAuthError,
        ToolCallValidationError,
        LoopBudgetExceededError,
        DuplicateCallDetectedError,
    }
    for cls in retryable:
        assert cls.retryable is True, cls
        assert issubclass(cls, AIError)
    for cls in not_retryable:
        assert cls.retryable is False, cls
        assert issubclass(cls, AIError)
    # Base class itself must fail safe (non-retryable) for any future subclass
    # that forgets to set the flag explicitly.
    assert AIError.retryable is False


def test_ai_settings_from_env_reads_existing_ai_env_vars(monkeypatch):
    monkeypatch.setenv("AI_PROVIDER", "openai_compatible")
    monkeypatch.setenv("AI_BASE_URL", "http://example.invalid/v1")
    monkeypatch.setenv("AI_MODEL", "some-model")
    monkeypatch.setenv("AI_API_KEY", "secret-key")
    monkeypatch.setenv("AI_TIMEOUT_SECONDS", "45")
    monkeypatch.setenv("EMBEDDING_DIMENSION", "384")

    settings = AISettings.from_env()

    assert settings.provider == "openai_compatible"
    assert settings.base_url == "http://example.invalid/v1"
    assert settings.model == "some-model"
    assert settings.api_key == "secret-key"
    assert settings.timeout_seconds == 45.0
    assert settings.embedding_dimension == 384


def test_ai_settings_from_env_defaults_match_current_hardcoded_defaults(monkeypatch):
    # Mirrors AnthropicCopilotProvider.__init__ and OpenAICompatibleCopilotProvider.__init__'s
    # own os.getenv(..., <default>) calls in app/copilot.py -- this test exists so a future
    # change to either provider's hardcoded default and this settings object's defaults can't
    # silently drift apart.
    for var in ("AI_PROVIDER", "AI_BASE_URL", "AI_MODEL", "AI_TIMEOUT_SECONDS"):
        monkeypatch.delenv(var, raising=False)

    monkeypatch.setenv("AI_PROVIDER", "anthropic")
    anthropic_defaults = AISettings.from_env()
    assert anthropic_defaults.base_url == "https://api.anthropic.com"
    assert anthropic_defaults.model == "claude-sonnet-5"
    assert anthropic_defaults.anthropic_version == "2023-06-01"

    monkeypatch.setenv("AI_PROVIDER", "openai_compatible")
    openai_compat_defaults = AISettings.from_env()
    assert openai_compat_defaults.base_url == "http://127.0.0.1:1234/v1"
    assert openai_compat_defaults.model == "local-model"

    # Deliberately NOT asserted as 20.0 here: unlike the two provider classes today
    # (which always resolve to a float via os.getenv(..., "20")), AISettings.timeout_seconds
    # is None when unset by design, so a caller can apply capability-profile-aware
    # precedence instead of a single flat fallback. See app/ai/config.py's docstring.
    assert openai_compat_defaults.timeout_seconds is None

    monkeypatch.setenv("AI_TIMEOUT_SECONDS", "99")
    assert AISettings.from_env().timeout_seconds == 99.0


def test_render_anthropic_and_render_openai_cover_same_names_as_canonical_specs():
    # Both dialect renderers must offer exactly the same tool NAMES as the
    # canonical spec list they were given -- no tool silently dropped or
    # duplicated by either render function.
    names = {spec["name"] for spec in TOOL_SPECS}
    anthropic_rendered = render_anthropic(TOOL_SPECS)
    openai_rendered = render_openai(TOOL_SPECS)
    assert {t["name"] for t in anthropic_rendered} == names
    assert {t["function"]["name"] for t in openai_rendered} == names
    # And the wrapper functions in copilot.py that add role-filtering on top
    # must still round-trip through these renderers correctly.
    assert {t["name"] for t in anthropic_tool_specs("admin")} <= names
    assert {t["function"]["name"] for t in openai_tool_specs("member")} <= names


def test_resolve_profile_matches_known_model_patterns():
    falcon = resolve_profile("openai_compatible", "falcon-h1-3b-instruct")
    assert falcon.needs_textual_adapter is True
    assert falcon.supports_native_tool_calls is False

    fabliq = resolve_profile("openai_compatible", "fabliq-8b-agent-reasoning@q4_k_m")
    assert fabliq.needs_textual_adapter is False
    assert fabliq.supports_native_tool_calls is True
    assert fabliq.reasoning_capable is True

    anthropic = resolve_profile("anthropic", "claude-sonnet-5")
    assert anthropic.needs_textual_adapter is False
    assert anthropic.supports_native_tool_calls is True


def test_resolve_profile_falls_back_to_default_for_unknown_model():
    # Fail-safe, not fail-open: an unrecognized model must never be assumed to
    # need (or not need) the textual adapter -- it gets the conservative
    # native-only default either way.
    assert resolve_profile("openai_compatible", "some-brand-new-model") is DEFAULT_PROFILE
    assert resolve_profile("some_future_provider", "whatever") is DEFAULT_PROFILE


def test_openai_compatible_provider_uses_profile_timeout_when_unset():
    settings = AISettings(
        provider="openai_compatible", base_url="http://x/v1", model="falcon-h1-3b-instruct",
        api_key="", timeout_seconds=None, anthropic_version="v", embedding_dimension=1536,
    )
    provider = OpenAICompatibleCopilotProvider(settings)
    assert provider.timeout == provider.profile.timeout_seconds == 180.0
    assert provider.profile.needs_textual_adapter is True


def test_openai_compatible_provider_prefers_explicit_AI_TIMEOUT_SECONDS_override():
    settings = AISettings(
        provider="openai_compatible", base_url="http://x/v1", model="falcon-h1-3b-instruct",
        api_key="", timeout_seconds=42.0, anthropic_version="v", embedding_dimension=1536,
    )
    provider = OpenAICompatibleCopilotProvider(settings)
    # Falcon's profile default is 180s -- the explicit override must win anyway.
    assert provider.timeout == 42.0
    assert provider.profile.timeout_seconds == 180.0


def test_anthropic_provider_timeout_resolution_same_precedence():
    unset = AISettings(
        provider="anthropic", base_url="https://api.anthropic.com", model="claude-sonnet-5",
        api_key="k", timeout_seconds=None, anthropic_version="2023-06-01", embedding_dimension=1536,
    )
    assert AnthropicCopilotProvider(unset).timeout == 20.0

    overridden = AISettings(
        provider="anthropic", base_url="https://api.anthropic.com", model="claude-sonnet-5",
        api_key="k", timeout_seconds=7.5, anthropic_version="2023-06-01", embedding_dimension=1536,
    )
    assert AnthropicCopilotProvider(overridden).timeout == 7.5


def _fake_urlopen_response(payload: dict):
    response = MagicMock()
    response.read.return_value = json.dumps(payload).encode()
    response.__enter__ = MagicMock(return_value=response)
    response.__exit__ = MagicMock(return_value=False)
    return response


def test_openai_compatible_provider_invokes_adapter_only_when_profile_requires_it():
    # The exact same leaked <tool_call> text from LM Studio, replayed against two
    # different resolved profiles -- only the one that needs_textual_adapter should
    # ever recover it into a real tool call.
    leaked_payload = {"choices": [{"message": {
        "content": '<tool_call>{"name": "get_projects", "arguments": {}}</tool_call>',
    }, "finish_reason": "stop"}]}

    falcon_settings = AISettings(
        provider="openai_compatible", base_url="http://x/v1", model="falcon-h1-3b-instruct",
        api_key="", timeout_seconds=1.0, anthropic_version="v", embedding_dimension=1536,
    )
    falcon_provider = OpenAICompatibleCopilotProvider(falcon_settings)
    assert falcon_provider.profile.needs_textual_adapter is True
    with patch("app.ai.providers.request.urlopen", return_value=_fake_urlopen_response(leaked_payload)):
        recovered_turn = falcon_provider.respond("system", [{"role": "user", "content": "hi"}])
    assert len(recovered_turn.tool_calls) == 1
    assert recovered_turn.tool_calls[0]["name"] == "get_projects"
    assert recovered_turn.adapter_used == "textual_v1"

    fabliq_settings = AISettings(
        provider="openai_compatible", base_url="http://x/v1", model="fabliq-8b-agent-reasoning",
        api_key="", timeout_seconds=1.0, anthropic_version="v", embedding_dimension=1536,
    )
    fabliq_provider = OpenAICompatibleCopilotProvider(fabliq_settings)
    assert fabliq_provider.profile.needs_textual_adapter is False
    with patch("app.ai.providers.request.urlopen", return_value=_fake_urlopen_response(leaked_payload)):
        unrecovered_turn = fabliq_provider.respond("system", [{"role": "user", "content": "hi"}])
    assert unrecovered_turn.tool_calls == []
    assert unrecovered_turn.adapter_used is None
    assert unrecovered_turn.text == '<tool_call>{"name": "get_projects", "arguments": {}}</tool_call>'


def test_openai_compatible_provider_does_not_invoke_adapter_when_native_tool_calls_present():
    # A model that already produced a real tool_calls entry must never have the
    # adapter run over its (empty) content -- the adapter is strictly a recovery
    # path for leaked text, never a second-guessing pass over a working response.
    native_payload = {"choices": [{"message": {
        "content": None,
        "tool_calls": [{"id": "1", "type": "function", "function": {"name": "get_projects", "arguments": "{}"}}],
    }, "finish_reason": "tool_calls"}]}
    falcon_settings = AISettings(
        provider="openai_compatible", base_url="http://x/v1", model="falcon-h1-3b-instruct",
        api_key="", timeout_seconds=1.0, anthropic_version="v", embedding_dimension=1536,
    )
    provider = OpenAICompatibleCopilotProvider(falcon_settings)
    with patch("app.ai.providers.request.urlopen", return_value=_fake_urlopen_response(native_payload)):
        turn = provider.respond("system", [{"role": "user", "content": "hi"}])
    assert turn.tool_calls == [{"id": "1", "name": "get_projects", "args": {}}]
    assert turn.adapter_used is None

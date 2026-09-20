"""Explicit per-model/provider capability profiles.

Replaces scattering `if model_name.startswith("falcon")`-style checks through
business logic with one lookup. `DEFAULT_PROFILE` is deliberately
conservative (native tool calls only, no compatibility adapter, today's 20s
timeout) so an unrecognized model fails safe rather than guessing at exotic
behavior. `PROFILES` is checked in order; the first (provider, model-pattern)
match wins, so more specific entries must come before their catch-alls.

Capabilities added for universal adapter support:
- native_tool_calls / textual_adapter / reasoning_channel / vision / structured_output
- supports_auto_tool_choice / supports_required_tool_choice / supports_none_tool_choice
- parallel_tool_calls / multiple_calls_per_turn / system_message_support
- context_window / practical_max_tools / tool_schema_dialect
- tool_result_ID_requirements / streaming_tools
- provider-level defaults and explicit overrides supported via config/env.
"""
from __future__ import annotations

import fnmatch
from dataclasses import dataclass


@dataclass(frozen=True)
class ModelProfile:
    name: str
    provider_families: tuple[str, ...]
    model_pattern: str
    # Tool-calling format
    supports_native_tool_calls: bool = True
    needs_textual_adapter: bool = False
    supports_auto_tool_choice: bool = True
    supports_required_tool_choice: bool = False
    supports_none_tool_choice: bool = False
    supports_multiple_calls_per_turn: bool = False
    parallel_tool_calls: bool = False
    # Channel / reasoning
    system_message_support: bool = True
    reasoning_capable: bool = False
    reasoning_channel: bool = False
    # Structured output / vision / streaming
    structured_output: bool = False
    vision: bool = False
    streaming_tools: bool = False
    # Capacity / limits
    timeout_seconds: float = 20.0
    max_tool_turns: int = 6
    context_window: int = 4096
    practical_max_tools: int = 10
    # Schema / wire conventions
    tool_schema_dialect: str = "openai"
    requires_tool_result_id: bool = True


DEFAULT_PROFILE = ModelProfile(
    name="default",
    provider_families=(),
    model_pattern="*",
    supports_native_tool_calls=True,
    needs_textual_adapter=False,
    supports_auto_tool_choice=True,
    supports_required_tool_choice=False,
    supports_none_tool_choice=False,
    supports_multiple_calls_per_turn=False,
    parallel_tool_calls=False,
    system_message_support=True,
    reasoning_capable=False,
    reasoning_channel=False,
    structured_output=False,
    vision=False,
    streaming_tools=False,
    timeout_seconds=20.0,
    max_tool_turns=6,
    context_window=4096,
    practical_max_tools=10,
    tool_schema_dialect="openai",
    requires_tool_result_id=True,
)

PROFILES: list[ModelProfile] = [
    ModelProfile(
        name="anthropic",
        provider_families=("anthropic",),
        model_pattern="claude-*",
        supports_native_tool_calls=True,
        needs_textual_adapter=False,
        supports_auto_tool_choice=True,
        supports_none_tool_choice=True,
        supports_multiple_calls_per_turn=True,
        parallel_tool_calls=True,
        system_message_support=True,
        reasoning_capable=False,
        reasoning_channel=False,
        structured_output=False,
        vision=False,
        streaming_tools=False,
        timeout_seconds=20.0,
        max_tool_turns=6,
        context_window=200000,
        practical_max_tools=20,
        tool_schema_dialect="anthropic",
        requires_tool_result_id=True,
    ),
    # Evidenced: falcon-h1-3b-instruct under LM Studio's tool_choice:"auto" almost
    # never produces native message.tool_calls, instead leaking a textual
    # <tool_call>{...}</tool_call> pseudo-call into message.content (0/6 native in
    # isolation, 6/23 = 26% in the real app). See
    # /home/kenz/falcon_h1_3b_eval/issues.md, finding C1.
    ModelProfile(
        name="falcon-h1",
        provider_families=("openai_compatible", "lmstudio"),
        model_pattern="falcon-h1*",
        supports_native_tool_calls=False,
        needs_textual_adapter=True,
        supports_auto_tool_choice=True,
        supports_required_tool_choice=True,
        supports_none_tool_choice=False,
        supports_multiple_calls_per_turn=False,
        parallel_tool_calls=False,
        system_message_support=True,
        reasoning_capable=False,
        reasoning_channel=False,
        structured_output=False,
        vision=False,
        streaming_tools=False,
        timeout_seconds=180.0,
        max_tool_turns=6,
        context_window=131072,
        practical_max_tools=8,
        tool_schema_dialect="openai",
        requires_tool_result_id=True,
    ),
    # Evidenced: fabliq-8b-agent-reasoning produces reliable native
    # message.tool_calls (5/5, 5/7 isolated; 16/23 = 70% in the real app) but is a
    # heavy "thinking" model -- 87.1% of completion tokens across the isolated
    # probe were reasoning tokens -- and can leak inert self-invented pseudo-tags
    # (<proposal>, <function-call>) into replies that are never real tool calls.
    ModelProfile(
        name="fabliq",
        provider_families=("openai_compatible", "lmstudio"),
        model_pattern="fabliq*",
        supports_native_tool_calls=True,
        needs_textual_adapter=False,
        supports_auto_tool_choice=True,
        supports_required_tool_choice=False,
        supports_none_tool_choice=False,
        supports_multiple_calls_per_turn=True,
        parallel_tool_calls=False,
        system_message_support=True,
        reasoning_capable=True,
        reasoning_channel=True,
        structured_output=True,
        vision=False,
        streaming_tools=False,
        timeout_seconds=60.0,
        max_tool_turns=6,
        context_window=8192,
        practical_max_tools=12,
        tool_schema_dialect="openai",
        requires_tool_result_id=True,
    ),
    # Union Alpha (temporary primary) profile — conservative, not model-specific.
    # Used when provider=openrouter and no exact model match found; treats it
    # as an unrecognized cloud model with native structured calls, standard
    # timeouts, and no adapter, unless overridden by config/env.
    ModelProfile(
        name="union-alpha",
        provider_families=("openrouter",),
        model_pattern="union*",
        supports_native_tool_calls=True,
        needs_textual_adapter=False,
        supports_auto_tool_choice=True,
        supports_required_tool_choice=False,
        supports_none_tool_choice=False,
        supports_multiple_calls_per_turn=False,
        parallel_tool_calls=False,
        system_message_support=True,
        reasoning_capable=True,
        reasoning_channel=True,
        structured_output=True,
        vision=False,
        streaming_tools=False,
        timeout_seconds=30.0,
        max_tool_turns=6,
        context_window=128000,
        practical_max_tools=15,
        tool_schema_dialect="openai",
        requires_tool_result_id=True,
    ),
]


def resolve_profile(provider: str, model: str) -> ModelProfile:
    provider = (provider or "").strip().lower()
    model = (model or "").strip().lower()
    for profile in PROFILES:
        if provider in profile.provider_families and fnmatch.fnmatch(model, profile.model_pattern):
            return profile
    return DEFAULT_PROFILE

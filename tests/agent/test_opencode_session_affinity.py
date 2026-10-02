"""Regression tests for OpenCode x-opencode-session affinity."""

from __future__ import annotations

from agent import auxiliary_client as aux
from agent.chat_completion_helpers import build_api_kwargs
from agent.opencode_affinity import (
    OPENCODE_SESSION_HEADER,
    merge_opencode_session_headers,
    opencode_session_headers,
)
from gateway.session_context import clear_session_vars, set_session_vars
from run_agent import AIAgent

_MSGS = [{"role": "user", "content": "hi"}]


def _agent(provider: str, model: str, base_url: str, api_mode: str | None = None):
    agent = AIAgent(
        api_key="test-key",
        base_url=base_url,
        model=model,
        provider=provider,
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
        session_id="sess-affinity-1",
    )
    if api_mode:
        agent.api_mode = api_mode
        agent._transport = None
        agent._anthropic_base_url = base_url
    return agent


def test_main_turn_adds_stable_header_for_opencode_chat_completions():
    agent = _agent("opencode-go", "glm-5", "https://opencode.ai/zen/go/v1")
    first = build_api_kwargs(agent, _MSGS)
    second = build_api_kwargs(agent, _MSGS)
    assert first["extra_headers"][OPENCODE_SESSION_HEADER] == "sess-affinity-1"
    assert second["extra_headers"][OPENCODE_SESSION_HEADER] == "sess-affinity-1"


def test_custom_opencode_url_is_detected_and_other_providers_are_untouched():
    assert opencode_session_headers(
        "custom", "https://opencode.ai/zen/go/v1", "sess-url"
    ) == {OPENCODE_SESSION_HEADER: "sess-url"}
    assert opencode_session_headers(
        "openrouter", "https://openrouter.ai/api/v1", "sess-other"
    ) == {}


def test_auxiliary_calls_use_task_local_ambient_hermes_session():
    tokens = set_session_vars(session_id="sess-ambient")
    try:
        kwargs = aux._build_call_kwargs(
            "opencode-go",
            "glm-5",
            _MSGS,
            base_url="https://opencode.ai/zen/go/v1",
        )
        assert kwargs["extra_headers"][OPENCODE_SESSION_HEADER] == "sess-ambient"
    finally:
        clear_session_vars(tokens)


def test_explicit_request_header_is_not_overwritten():
    kwargs = {"extra_headers": {OPENCODE_SESSION_HEADER: "caller-pinned"}}
    merged = merge_opencode_session_headers(
        kwargs,
        "opencode-go",
        "https://opencode.ai/zen/go/v1",
        "sess-generated",
    )
    assert merged["extra_headers"][OPENCODE_SESSION_HEADER] == "caller-pinned"

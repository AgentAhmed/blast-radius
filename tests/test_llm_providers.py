"""Provider slots: Claude adapter conversion, private-field stripping, error mapping across SDKs (no network)."""
import json
from types import SimpleNamespace

import anthropic
import openai

from blastradius.llm import LLMGateway, Provider, load_providers
from blastradius.llm.anthropic_adapter import to_anthropic_messages, to_anthropic_tool
from blastradius.llm.gateway import _classify

try:
    import httpx2 as httpx
except ImportError:
    import httpx

THINKING = {"type": "thinking", "thinking": "", "signature": "sig-abc"}


def _err(cls, status, headers=None):
    req = httpx.Request("POST", "http://llm.test")
    return cls("boom", response=httpx.Response(status, headers=headers or {}, request=req), body=None)


def test_every_slot_is_a_template_until_its_key_is_set(monkeypatch):
    for k in ("GROQ_API_KEY", "GEMINI_API_KEY", "OPENROUTER_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY",
              "CUSTOM_LLM_API_KEY", "LLM_PROVIDER_ORDER"):
        monkeypatch.delenv(k, raising=False)
    providers = {p.name: p for p in load_providers()}
    assert list(providers) == ["groq", "gemini", "openrouter", "openai", "anthropic", "custom", "ollama"]
    assert not any(p.configured for n, p in providers.items() if n != "ollama")
    assert providers["ollama"].configured            # local: no key needed (skipped at call time if not running)

    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    monkeypatch.setenv("ANTHROPIC_MODELS", "claude-sonnet-5-5,claude-opus-5-5")
    claude = {p.name: p for p in load_providers()}["anthropic"]
    assert claude.configured and claude.kind == "anthropic" and claude.models == ["claude-sonnet-5-5", "claude-opus-5-5"]
    assert not claude.send_temperature


def test_history_converts_to_claude_turns_and_keeps_thinking_blocks():
    claude_turn = {"role": "assistant", "content": "", "_anthropic": [
        THINKING, {"type": "tool_use", "id": "t1", "name": "blast_radius", "input": {"component": "payment"}}]}
    other_turn = {"role": "assistant", "content": "checking", "tool_calls": [
        {"id": "t2", "type": "function", "function": {"name": "past_fixes", "arguments": '{"component": "payment"}'}},
        {"id": "t3", "type": "function", "function": {"name": "experts", "arguments": '{"component": "payment"}'}}]}
    history = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "investigate"},
        claude_turn,
        {"role": "tool", "tool_call_id": "t1", "content": '{"x": 1}'},
        other_turn,
        {"role": "tool", "tool_call_id": "t2", "content": "{}"},
        {"role": "tool", "tool_call_id": "t3", "content": "{}"},
        {"role": "user", "content": "Step budget reached."},
    ]
    system, turns = to_anthropic_messages(history)
    assert system == "sys"
    assert [t["role"] for t in turns] == ["user", "assistant", "user", "assistant", "user"]
    assert turns[1]["content"] == claude_turn["_anthropic"]                 # thinking passed back unchanged
    assert turns[3]["content"][1] == {"type": "tool_use", "id": "t2", "name": "past_fixes",
                                      "input": {"component": "payment"}}
    last = turns[4]["content"]                                               # parallel results in ONE user turn,
    assert [b["type"] for b in last] == ["tool_result", "tool_result", "text"]   # results first, then the note
    assert to_anthropic_messages(history) == (system, turns)                # deterministic for resume


def test_tool_schema_conversion():
    tool = {"type": "function", "function": {"name": "who_to_page", "description": "d",
                                             "parameters": {"type": "object", "properties": {}}}}
    assert to_anthropic_tool(tool) == {"name": "who_to_page", "description": "d",
                                       "input_schema": {"type": "object", "properties": {}}}


def test_claude_only_fields_are_stripped_and_temperature_skipped_where_unsupported(monkeypatch):
    monkeypatch.setenv("A_KEY", "x")
    sent = []

    def fake_client(p):
        def create(**kw):
            sent.append(kw)
            msg = SimpleNamespace(content="ok", tool_calls=None)
            return SimpleNamespace(choices=[SimpleNamespace(message=msg)], usage=None)
        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))

    p = Provider("a", "http://a", ["m"], api_key_env="A_KEY", send_temperature=False)
    LLMGateway([p], client_factory=fake_client).chat([{"role": "assistant", "content": "", "_anthropic": [THINKING]}])
    assert sent[0]["messages"] == [{"role": "assistant", "content": ""}]
    assert "temperature" not in sent[0]


def test_errors_from_both_sdks_map_to_the_same_decisions():
    for sdk in (openai, anthropic):
        assert _classify(_err(sdk.RateLimitError, 429, {"retry-after": "12"})) == ("rate limited", "model", 12.0)
        assert _classify(_err(sdk.AuthenticationError, 401))[1:] == ("provider", float("inf"))
        assert _classify(_err(sdk.NotFoundError, 404))[1] == "model"
        assert _classify(_err(sdk.BadRequestError, 400)) == ("bad request", None, None)
        assert _classify(_err(sdk.InternalServerError, 500))[0] == "provider unavailable"
    assert _classify(_err(anthropic.APIStatusError, 529))[1] == "provider"      # overloaded


def test_claude_tool_calls_come_back_in_openai_shape():
    from blastradius.llm.anthropic_adapter import to_openai_response
    raw = [THINKING, {"type": "text", "text": "Looking."},
           {"type": "tool_use", "id": "t9", "name": "blast_radius", "input": {"component": "cart"}}]
    resp = SimpleNamespace(to_dict=lambda: {"content": raw},
                           usage=SimpleNamespace(input_tokens=10, output_tokens=5))
    out = to_openai_response(resp)
    msg = out.choices[0].message
    assert msg.content == "Looking." and msg.raw_blocks == raw
    assert msg.tool_calls[0].id == "t9" and json.loads(msg.tool_calls[0].function.arguments) == {"component": "cart"}
    assert out.usage.model_dump()["total_tokens"] == 15

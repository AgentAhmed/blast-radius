"""Claude slot: calls the Messages API through the official Anthropic SDK and gives the gateway the same
shape an OpenAI-compatible client returns (choices[0].message.content / .tool_calls, usage).

The agents keep their conversation in OpenAI chat format, so this module converts in both directions:
  system message          -> `system`
  user / tool messages    -> user turns; consecutive tool results go into ONE user turn
  assistant messages      -> the exact content blocks Claude returned earlier (kept on the message as
                             `_anthropic`), so thinking blocks are passed back unchanged as Claude requires;
                             assistant turns written by other providers become text + tool_use blocks
  tools                   -> {name, description, input_schema}

Claude Opus 5.5 always thinks (effort controls how much) and takes no temperature. Server-side refusal
fallbacks are on: if the model declines, the API retries on a fallback model inside the same call.
"""
import json
from types import SimpleNamespace

import anthropic

MAX_TOKENS = 16000          # thinking counts toward max_tokens, so leave room beyond the visible reply
EFFORT = "medium"           # agent tool loops: enough depth to weigh evidence, cheaper than high
FALLBACK_BETA = "server-side-fallback-2026-07-01"


class ModelRefusal(Exception):
    """The model (and its fallback) declined the request; the gateway moves on to the next slot."""


class AnthropicChatAdapter:
    def __init__(self, provider):
        self._client = anthropic.Anthropic(api_key=provider.api_key, max_retries=0, timeout=120)
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, model, messages, tools=None, max_tokens=None, extra_headers=None, **_ignored):
        system, turns = to_anthropic_messages(messages)
        kwargs = {
            "model": model,
            "max_tokens": max(max_tokens or 0, MAX_TOKENS),
            "messages": turns,
            "output_config": {"effort": EFFORT},
            "betas": [FALLBACK_BETA],
            "fallbacks": "default",
        }
        if system:
            kwargs["system"] = system
        if tools:
            kwargs["tools"] = [to_anthropic_tool(t) for t in tools]
        if extra_headers:
            kwargs["extra_headers"] = extra_headers
        response = self._client.beta.messages.create(**kwargs)
        if response.stop_reason == "refusal":
            category = getattr(response.stop_details, "category", None) if response.stop_details else None
            raise ModelRefusal(f"declined ({category or 'no category'})")
        return to_openai_response(response)


def to_anthropic_tool(tool: dict) -> dict:
    fn = tool["function"]
    return {"name": fn["name"], "description": fn.get("description", ""), "input_schema": fn["parameters"]}


def to_anthropic_messages(messages: list[dict]) -> tuple[str, list[dict]]:
    """OpenAI-format history -> (system prompt, Anthropic turns). Deterministic: the same history always
    produces byte-identical turns, which keeps earlier thinking blocks valid when a run is resumed."""
    system_parts, turns = [], []

    def user_blocks() -> list:
        if not turns or turns[-1]["role"] != "user":
            turns.append({"role": "user", "content": []})
        return turns[-1]["content"]

    for m in messages:
        role = m["role"]
        if role == "system":
            if turns:   # a later operator instruction becomes a user-visible note (never edits the prefix)
                user_blocks().append({"type": "text", "text": m["content"]})
            else:
                system_parts.append(m["content"])
        elif role == "user":
            user_blocks().append({"type": "text", "text": m["content"]})
        elif role == "tool":
            user_blocks().append({"type": "tool_result", "tool_use_id": m["tool_call_id"],
                                  "content": m["content"] or "{}"})
        elif role == "assistant":
            if m.get("_anthropic"):
                content = m["_anthropic"]
            else:   # written by another provider: rebuild as text + tool_use blocks
                content = [{"type": "text", "text": m["content"]}] if m.get("content") else []
                for c in m.get("tool_calls") or []:
                    content.append({"type": "tool_use", "id": c["id"], "name": c["function"]["name"],
                                    "input": json.loads(c["function"].get("arguments") or "{}")})
            turns.append({"role": "assistant", "content": content})
    return "\n\n".join(system_parts), turns


def to_openai_response(response) -> SimpleNamespace:
    raw = response.to_dict()["content"]
    text = "".join(b.get("text", "") for b in raw if b["type"] == "text")
    tool_calls = [
        SimpleNamespace(id=b["id"], function=SimpleNamespace(name=b["name"], arguments=json.dumps(b["input"])))
        for b in raw if b["type"] == "tool_use"
    ]
    message = SimpleNamespace(content=text, tool_calls=tool_calls or None, raw_blocks=raw)
    usage = response.usage
    tokens = (usage.input_tokens or 0) + (usage.output_tokens or 0)
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message)],
        usage=SimpleNamespace(model_dump=lambda: {"input_tokens": usage.input_tokens,
                                                  "output_tokens": usage.output_tokens,
                                                  "total_tokens": tokens}),
    )

"""LLM gateway: one call that tries providers and models in order until one answers.

How a call is routed
1. Skip providers with no key (template slots) and anything in cooldown.
2. Respect our own requests-per-minute budget per provider (wait briefly, or move on).
3. On failure, decide what to do from the error:
     429 rate limit      -> cool that model down for `retry-after` seconds, try next
     5xx / timeout / down-> cool the whole provider down for a short time, try next
     401/403 bad key     -> disable the provider for this process, try next
     404 unknown model   -> disable that model, try next
     400 bad request     -> try the next model (e.g. a model that produced invalid tool calls)
     refusal (Claude)    -> try the next model
4. If nothing can answer, raise AllProvidersExhausted with the earliest time a retry makes sense,
   so the caller can checkpoint and resume later instead of failing.

OpenAI-compatible slots and the Claude slot raise different exception classes (openai.* / anthropic.*);
_classify maps both to the same decisions.
"""
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable

import anthropic
import openai

from blastradius.llm.anthropic_adapter import AnthropicChatAdapter, ModelRefusal
from blastradius.llm.providers import Provider, load_providers

log = logging.getLogger("blastradius.llm")

FOREVER = float("inf")
PROVIDER_DOWN_COOLDOWN = 30      # seconds
DEFAULT_RATE_LIMIT_COOLDOWN = 60
MAX_RPM_WAIT = 5                 # wait this long at most for our own RPM budget before moving on


class AllProvidersExhausted(Exception):
    def __init__(self, resume_at: float | None, attempts: list[dict]):
        self.resume_at = resume_at
        self.attempts = attempts
        when = f"retry after {time.strftime('%H:%M:%S', time.localtime(resume_at))}" if resume_at else "no provider configured"
        super().__init__(f"All LLM providers unavailable ({when})")


@dataclass
class LLMResult:
    message: object            # OpenAI-style assistant message (content / tool_calls)
    provider: str
    model: str
    usage: dict
    latency_ms: int
    attempts: list[dict] = field(default_factory=list)   # every failed try before this one


class _RpmLimiter:
    """Sliding one-minute window: at most `rpm` requests in any 60 seconds."""

    def __init__(self, rpm: int, clock: Callable[[], float]):
        self.rpm, self.clock, self.sent = rpm, clock, deque()

    def wait_time(self) -> float:
        now = self.clock()
        while self.sent and now - self.sent[0] >= 60:
            self.sent.popleft()
        return 0.0 if len(self.sent) < self.rpm else 60 - (now - self.sent[0])

    def record(self) -> None:
        self.sent.append(self.clock())


class LLMGateway:
    def __init__(self, providers: list[Provider] | None = None,
                 client_factory: Callable[[Provider], object] | None = None,
                 clock: Callable[[], float] = time.time,
                 sleep: Callable[[float], None] = time.sleep):
        self.providers = providers if providers is not None else load_providers()
        self._client_factory = client_factory or self._openai_client
        self._clients: dict[str, object] = {}
        self._clock, self._sleep = clock, sleep
        self._limiters = {p.name: _RpmLimiter(p.rpm, clock) for p in self.providers}
        self._cooldown: dict[tuple[str, str | None], float] = {}   # (provider, model|None) -> until

    # -- public -------------------------------------------------------------------------
    def chat(self, messages: list[dict], tools: list[dict] | None = None,
             temperature: float = 0.2, max_tokens: int | None = None) -> LLMResult:
        attempts: list[dict] = []
        for p in self.providers:
            if not p.configured:
                continue
            for model in p.models:
                if self._cooling(p.name, model):
                    continue
                if not self._within_rpm(p):
                    attempts.append({"provider": p.name, "model": model, "error": "local rpm budget"})
                    break
                result = self._try(p, model, messages, tools, temperature, max_tokens, attempts)
                if result:
                    result.attempts = attempts
                    return result
                if self._cooling(p.name, None):
                    break   # whole provider is down; skip its other models
        raise AllProvidersExhausted(self._earliest_retry(), attempts)

    def status(self) -> list[dict]:
        now = self._clock()
        out = []
        for p in self.providers:
            cd = {m or "*": until for (name, m), until in self._cooldown.items() if name == p.name and until > now}
            out.append({"provider": p.name, "configured": p.configured, "models": p.models,
                        "rpm": p.rpm, "local": p.local, "cooldowns": cd})
        return out

    # -- internals ----------------------------------------------------------------------
    def _try(self, p, model, messages, tools, temperature, max_tokens, attempts) -> LLMResult | None:
        client = self._client(p)
        if p.kind == "openai":   # fields starting with "_" (e.g. Claude's raw blocks) are ours, not the API's
            messages = [{k: v for k, v in m.items() if not k.startswith("_")} for m in messages]
        kwargs = {"model": model, "messages": messages}
        if p.send_temperature:
            kwargs["temperature"] = temperature
        if tools:
            kwargs["tools"] = tools
        if max_tokens:
            kwargs["max_tokens"] = max_tokens
        if p.extra_headers:
            kwargs["extra_headers"] = p.extra_headers
        start = self._clock()
        self._limiters[p.name].record()
        try:
            resp = client.chat.completions.create(**kwargs)
        except _API_ERRORS + (ModelRefusal,) as e:
            reason, scope, seconds = _classify(e)
            if seconds:
                self._cool(p.name, model if scope == "model" else None, seconds)
            return self._failed(attempts, p, model, reason, e)

        usage = resp.usage.model_dump() if getattr(resp, "usage", None) else {}
        return LLMResult(message=resp.choices[0].message, provider=p.name, model=model, usage=usage,
                         latency_ms=int((self._clock() - start) * 1000))

    def _failed(self, attempts, p, model, reason, err) -> None:
        log.warning("LLM %s/%s failed: %s (%s)", p.name, model, reason, type(err).__name__)
        attempts.append({"provider": p.name, "model": model, "error": reason})
        return None

    def _within_rpm(self, p: Provider) -> bool:
        wait = self._limiters[p.name].wait_time()
        if wait == 0:
            return True
        if wait <= MAX_RPM_WAIT:
            self._sleep(wait)
            return True
        self._cool(p.name, None, wait)
        return False

    def _cool(self, provider: str, model: str | None, seconds: float) -> None:
        self._cooldown[(provider, model)] = self._clock() + seconds

    def _cooling(self, provider: str, model: str | None) -> bool:
        now = self._clock()
        if self._cooldown.get((provider, None), 0) > now:
            return True
        return model is not None and self._cooldown.get((provider, model), 0) > now

    def _earliest_retry(self) -> float | None:
        now = self._clock()
        times = [u for u in self._cooldown.values() if now < u < FOREVER]
        return min(times) if times else None

    def _client(self, p: Provider):
        if p.name not in self._clients:
            self._clients[p.name] = self._client_factory(p)
        return self._clients[p.name]

    @staticmethod
    def _openai_client(p: Provider):
        if p.kind == "anthropic":
            return AnthropicChatAdapter(p)
        return openai.OpenAI(base_url=p.base_url, api_key=p.api_key or "not-needed",
                             timeout=60 if not p.local else 180, max_retries=0)


_API_ERRORS = (openai.APIError, anthropic.APIError)


def _is(err, name: str) -> bool:
    """Same-named exception class in either SDK (both follow the same naming)."""
    return isinstance(err, (getattr(openai, name, ()), getattr(anthropic, name, ())))


def _classify(err) -> tuple[str, str | None, float | None]:
    """(reason, cooldown scope 'model'/'provider'/None, cooldown seconds). Most specific first."""
    if isinstance(err, ModelRefusal):
        return f"refused: {err}", None, None
    if _is(err, "RateLimitError"):
        return "rate limited", "model", _retry_after(err) or DEFAULT_RATE_LIMIT_COOLDOWN
    if _is(err, "AuthenticationError"):
        return "bad api key", "provider", FOREVER
    if _is(err, "PermissionDeniedError"):
        return "permission denied", "provider", FOREVER
    if _is(err, "NotFoundError"):
        return "model not found", "model", FOREVER
    if _is(err, "BadRequestError"):
        return "bad request", None, None
    if _is(err, "APIConnectionError") or _is(err, "APITimeoutError") or _is(err, "InternalServerError"):
        return "provider unavailable", "provider", PROVIDER_DOWN_COOLDOWN
    status = getattr(err, "status_code", None)
    return (f"http {status}" if status else "provider error"), "provider", PROVIDER_DOWN_COOLDOWN


def _retry_after(err) -> float | None:
    try:
        value = err.response.headers.get("retry-after")
        return float(value) if value else None
    except (AttributeError, ValueError):
        return None

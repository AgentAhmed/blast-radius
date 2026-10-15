"""Fallback logic of the LLM gateway, with fake providers (no network, no keys)."""
from types import SimpleNamespace

import openai
import pytest

try:  # openai>=3 ships its own transport package
    import httpx2 as httpx
except ImportError:
    import httpx
from blastradius.llm import AllProvidersExhausted, LLMGateway, Provider


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


def _status_error(cls, status, headers=None):
    req = httpx.Request("POST", "http://llm.test")
    return cls("boom", response=httpx.Response(status, headers=headers or {}, request=req), body=None)


def _ok(text="pong"):
    msg = SimpleNamespace(content=text, tool_calls=None)
    return SimpleNamespace(choices=[SimpleNamespace(message=msg)], usage=None)


class FakeClient:
    """Answers per model from a script: an exception to raise, or a response to return."""

    def __init__(self, script, calls):
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))
        self.script, self.calls = script, calls

    def _create(self, model, **_):
        self.calls.append(model)
        outcome = self.script.get(model, _ok(model))
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def make(providers, script, monkeypatch, keys=("A_KEY", "B_KEY")):
    for k in keys:
        monkeypatch.setenv(k, "test")
    calls, clock = [], FakeClock()
    gw = LLMGateway(providers, client_factory=lambda p: FakeClient(script, calls), clock=clock, sleep=clock.sleep)
    return gw, calls, clock


A = Provider("a", "http://a", ["a1", "a2"], api_key_env="A_KEY", rpm=100)
B = Provider("b", "http://b", ["b1"], api_key_env="B_KEY", rpm=100)
NOKEY = Provider("template", "http://t", ["t1"], api_key_env="MISSING_KEY_FOR_TEST")
LOCAL = Provider("local", "http://localhost", ["l1"], local=True)


def test_first_configured_provider_answers(monkeypatch):
    gw, calls, _ = make([NOKEY, A, B], {}, monkeypatch)
    r = gw.chat([{"role": "user", "content": "hi"}])
    assert (r.provider, r.model) == ("a", "a1")
    assert calls == ["a1"]          # template slot without key was skipped, never called


def test_rate_limit_falls_to_next_model_then_cools_down(monkeypatch):
    script = {"a1": _status_error(openai.RateLimitError, 429, {"retry-after": "20"})}
    gw, calls, clock = make([A, B], script, monkeypatch)
    r = gw.chat([])
    assert (r.provider, r.model) == ("a", "a2")
    assert r.attempts == [{"provider": "a", "model": "a1", "error": "rate limited"}]
    gw.chat([])
    assert calls == ["a1", "a2", "a2"]   # a1 skipped while cooling down
    clock.t += 21
    gw.chat([])
    assert calls[-2:] == ["a1", "a2"]    # a1 retried after retry-after (still limited in this script)


def test_provider_outage_skips_its_other_models(monkeypatch):
    req = httpx.Request("POST", "http://a")
    gw, calls, _ = make([A, B], {"a1": openai.APIConnectionError(request=req)}, monkeypatch)
    r = gw.chat([])
    assert r.provider == "b" and calls == ["a1", "b1"]


def test_bad_key_disables_provider(monkeypatch):
    gw, calls, _ = make([A, B], {"a1": _status_error(openai.AuthenticationError, 401)}, monkeypatch)
    gw.chat([])
    gw.chat([])
    assert calls == ["a1", "b1", "b1"]


def test_local_model_is_last_resort(monkeypatch):
    script = {m: _status_error(openai.RateLimitError, 429) for m in ("a1", "a2", "b1")}
    gw, _, _ = make([A, B, LOCAL], script, monkeypatch)
    assert gw.chat([]).provider == "local"


def test_everything_exhausted_reports_when_to_resume(monkeypatch):
    script = {m: _status_error(openai.RateLimitError, 429, {"retry-after": "45"}) for m in ("a1", "a2", "b1")}
    gw, _, clock = make([A, B], script, monkeypatch)
    with pytest.raises(AllProvidersExhausted) as e:
        gw.chat([])
    assert e.value.resume_at == clock.t + 45
    assert len(e.value.attempts) == 3


def test_own_rpm_budget_moves_to_next_provider(monkeypatch):
    tight = Provider("a", "http://a", ["a1"], api_key_env="A_KEY", rpm=1)
    gw, calls, _ = make([tight, B], {}, monkeypatch)
    gw.chat([])
    assert gw.chat([]).provider == "b"
    assert calls == ["a1", "b1"]


def test_nothing_configured(monkeypatch):
    gw, _, _ = make([NOKEY], {}, monkeypatch, keys=())
    with pytest.raises(AllProvidersExhausted) as e:
        gw.chat([])
    assert e.value.resume_at is None

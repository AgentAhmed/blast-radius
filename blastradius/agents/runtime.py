"""The agent loop: LLM picks tools, we run them, everything is checkpointed in FalkorDB.

    start() -> creates an AgentRun node, then execute()
    execute() -> rebuilds the conversation from the run's Step nodes and continues it

Because the conversation is rebuilt from the graph on every execute(), the same function handles a
fresh run, a run paused by LLM rate limits and a run interrupted by a crash. Completed steps are
never repeated; tool calls the model asked for but that never ran are run first.
"""
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Callable

from blastradius.agents import store
from blastradius.agents.tools import Tool, ToolError
from blastradius.llm import AllProvidersExhausted, LLMGateway

log = logging.getLogger("blastradius.agents")

DEFAULT_PAUSE = 60  # seconds, when providers give no retry hint


@dataclass
class AgentSpec:
    name: str
    tools: dict[str, Tool]
    max_llm_calls: int = 8
    accept_text_answer: bool = False                 # plain text reply counts as the final answer
    validate: Callable[[str, dict], None] | None = None   # raise ToolError to reject a terminal result
    on_finish: Callable[[str, dict, dict], None] | None = None  # (tenant, run, result)
    temperature: float = 0.1
    extra: dict = field(default_factory=dict)

    @property
    def terminal(self) -> Tool:
        return next(t for t in self.tools.values() if t.terminal)


_gateway: LLMGateway | None = None


def gateway() -> LLMGateway:
    global _gateway
    if _gateway is None:
        _gateway = LLMGateway()
    return _gateway


def start(tenant: str, spec: AgentSpec, goal: str, system: str, user: str, *, incident: str | None = None,
          task: str | None = None, session: str | None = None, gw: LLMGateway | None = None) -> dict:
    run_id = store.create_run(tenant, spec.name, goal, system, user, incident=incident, task=task, session=session)
    return execute(tenant, spec, run_id, gw)


def execute(tenant: str, spec: AgentSpec, run_id: str, gw: LLMGateway | None = None) -> dict:
    gw = gw or gateway()
    run = store.load_run(tenant, run_id)
    if run["status"] in ("done", "failed"):
        return {"run_id": run_id, "status": run["status"], "result": run.get("result")}
    store.set_status(tenant, run_id, "running")

    messages = _rebuild(run)
    steps = run["steps"]
    n = len(steps)
    llm_calls = sum(1 for s in steps if s["kind"] == "llm")
    schemas = [t.schema() for t in spec.tools.values()]

    while True:
        # 1. Run tool calls the model asked for that have no result yet (also covers crash recovery).
        for call in _pending_calls(messages):
            n += 1
            result, final = _run_tool(tenant, spec, run_id, n, call)
            messages.append({"role": "tool", "tool_call_id": call["id"], "content": json.dumps(result, default=str)})
            if final is not None:
                return _finish(tenant, spec, run_id, final)

        # 2. Out of budget: ask once more for the final answer, then give up.
        if llm_calls >= spec.max_llm_calls + 1:
            store.set_status(tenant, run_id, "failed", error="step budget exhausted")
            return {"run_id": run_id, "status": "failed", "result": None}
        if llm_calls == spec.max_llm_calls:
            n += 1
            nudge = f"Step budget reached. Call {spec.terminal.name} now with what you know."
            store.add_step(tenant, run_id, n, "note", message={"role": "user", "content": nudge})
            messages.append({"role": "user", "content": nudge})

        # 3. Ask the model what to do next.
        try:
            r = gw.chat(messages, tools=schemas, temperature=spec.temperature)
        except AllProvidersExhausted as e:
            resume_at = int(e.resume_at) if e.resume_at else int(time.time()) + DEFAULT_PAUSE
            n += 1
            store.add_step(tenant, run_id, n, "pause", output={"reason": str(e), "attempts": e.attempts})
            store.set_status(tenant, run_id, "paused", resume_at=resume_at, error=str(e))
            log.warning("run %s paused until %s", run_id, resume_at)
            return {"run_id": run_id, "status": "paused", "resume_at": resume_at}

        llm_calls += 1
        n += 1
        msg = _assistant_dict(r.message)
        store.add_step(tenant, run_id, n, "llm", message=msg, provider=r.provider, model=r.model,
                       tokens=(r.usage or {}).get("total_tokens"), latency_ms=r.latency_ms,
                       output={"fallbacks": r.attempts} if r.attempts else None)
        store.set_status(tenant, run_id, "running", provider=f"{r.provider}/{r.model}")
        messages.append(msg)

        if not msg.get("tool_calls"):
            text = (msg.get("content") or "").strip()
            if spec.accept_text_answer and text:
                return _finish(tenant, spec, run_id, {"answer": text, "sources": []})
            n += 1
            nudge = f"Use the tools. When done, call {spec.terminal.name}."
            store.add_step(tenant, run_id, n, "note", message={"role": "user", "content": nudge})
            messages.append({"role": "user", "content": nudge})


def _run_tool(tenant, spec: AgentSpec, run_id, n, call) -> tuple[dict, dict | None]:
    name = call["function"]["name"]
    tool = spec.tools.get(name)
    final = None
    try:
        args = json.loads(call["function"].get("arguments") or "{}")
        if tool is None:
            raise ToolError(f"Unknown tool '{name}'. Available: {', '.join(spec.tools)}")
        result = tool.fn(tenant, **args)
        if not tool.terminal:
            result = humanize_times(result)
        if tool.terminal:
            if spec.validate:
                spec.validate(tenant, result)
            final = result
    except ToolError as e:
        args = args if "args" in locals() else {}
        result = {"error": str(e)}
    except (json.JSONDecodeError, TypeError) as e:
        args = {}
        result = {"error": f"Invalid arguments for {name}: {e}"}
    touched = store.entities_in(tenant, {"args": args, "result": result})
    store.add_step(tenant, run_id, n, "tool", tool=name, args=args, output=result,
                   tool_call_id=call["id"], touched=touched)
    return result, final


def _finish(tenant, spec: AgentSpec, run_id, result: dict) -> dict:
    run = store.load_run(tenant, run_id)
    if spec.on_finish:
        spec.on_finish(tenant, run, result)
    store.set_status(tenant, run_id, "done", result=result)
    return {"run_id": run_id, "status": "done", "result": result}


TIME_KEYS = ("_at", "date", "until", "last_change")


def humanize_times(value, now: int | None = None):
    """Replace epoch-second timestamps with relative ages ('2h ago', 'in 3d'); LLMs misread raw epochs."""
    now = now or int(time.time())
    if isinstance(value, dict):
        return {k: (_ago(v, now) if isinstance(v, (int, float)) and v > 1e9 and k.endswith(TIME_KEYS)
                    else humanize_times(v, now)) for k, v in value.items()}
    if isinstance(value, list):
        return [humanize_times(v, now) for v in value]
    return value


def _ago(ts: float, now: int) -> str:
    delta = now - ts
    span = abs(delta)
    text = f"{int(span // 60)}m" if span < 3600 else f"{int(span // 3600)}h" if span < 86400 else f"{int(span // 86400)}d"
    return f"{text} ago" if delta >= 0 else f"in {text}"


def _rebuild(run: dict) -> list[dict]:
    messages = [{"role": "system", "content": run["system_prompt"]},
                {"role": "user", "content": run["user_prompt"]}]
    for s in run["steps"]:
        if s["kind"] in ("llm", "note") and s.get("message"):
            messages.append(json.loads(s["message"]))
        elif s["kind"] == "tool":
            messages.append({"role": "tool", "tool_call_id": s["tool_call_id"], "content": s.get("output") or "{}"})
    return messages


def _pending_calls(messages: list[dict]) -> list[dict]:
    last = next((i for i in range(len(messages) - 1, -1, -1) if messages[i]["role"] == "assistant"), None)
    if last is None:
        return []
    done = {m["tool_call_id"] for m in messages[last + 1:] if m["role"] == "tool"}
    pending = [c for c in messages[last].get("tool_calls") or [] if c["id"] not in done]
    # A final-answer tool called in the same turn as other tools runs last.
    return sorted(pending, key=lambda c: c["function"]["name"] in TERMINAL_NAMES)


TERMINAL_NAMES = {"submit_findings", "final_answer", "write_postmortem"}


def _assistant_dict(message) -> dict:
    out = {"role": "assistant", "content": message.content or ""}
    if getattr(message, "tool_calls", None):
        out["tool_calls"] = [{"id": tc.id, "type": "function",
                              "function": {"name": tc.function.name, "arguments": tc.function.arguments or "{}"}}
                             for tc in message.tool_calls]
    return out

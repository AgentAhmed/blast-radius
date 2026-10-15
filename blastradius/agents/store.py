"""Agent state in FalkorDB: runs, steps, tasks.

(:AgentRun)-[:HAS_STEP]->(:Step)-[:NEXT]->(:Step)    ordered trail of everything the agent did
(:Step)-[:TOUCHED]->(any node)                        which graph nodes a step read or wrote
(:Task)-[:FOR]->(:Incident)                           work handed from one agent to another
(:AgentRun)-[:WORKED_ON]->(:Task)

The step trail is the checkpoint: a paused or crashed run is rebuilt from its steps and continues
from the last completed one.
"""
import json
import re
import time
import uuid
from functools import lru_cache

from blastradius import db

MAX_OUTPUT_CHARS = 6000

CREATE_RUN = """
CREATE (r:AgentRun {id: $id, agent: $agent, goal: $goal, status: 'running', started_at: $now,
                    updated_at: $now, system_prompt: $system, user_prompt: $user, session: $session,
                    steps: 0})
WITH r
OPTIONAL MATCH (i:Incident {id: $incident})
FOREACH (_ IN CASE WHEN i IS NULL THEN [] ELSE [1] END | MERGE (r)-[:INVESTIGATES]->(i))
WITH r
OPTIONAL MATCH (t:Task {id: $task})
FOREACH (_ IN CASE WHEN t IS NULL THEN [] ELSE [1] END | MERGE (r)-[:WORKED_ON]->(t) SET t.run_id = $id)
RETURN r.id AS id
"""

ADD_STEP = """
MATCH (r:AgentRun {id: $run})
CREATE (s:Step {id: $id, run: $run, n: $n, kind: $kind, agent: r.agent, tool: $tool, args: $args,
                output: $output, message: $message, tool_call_id: $tool_call_id, provider: $provider,
                model: $model, tokens: $tokens, latency_ms: $latency_ms, at: $now})
CREATE (r)-[:HAS_STEP]->(s)
SET r.steps = $n, r.updated_at = $now
WITH r, s
OPTIONAL MATCH (r)-[:HAS_STEP]->(prev:Step {n: $n - 1})
FOREACH (_ IN CASE WHEN prev IS NULL THEN [] ELSE [1] END | MERGE (prev)-[:NEXT]->(s))
RETURN s.id AS id
"""

SET_STATUS = """
MATCH (r:AgentRun {id: $id})
SET r.status = $status, r.updated_at = $now, r.resume_at = $resume_at, r.error = $error,
    r.result = coalesce($result, r.result), r.provider = coalesce($provider, r.provider)
"""

LOAD_RUN = """
MATCH (r:AgentRun {id: $id})
OPTIONAL MATCH (r)-[:HAS_STEP]->(s:Step)
WITH r, s ORDER BY s.n
OPTIONAL MATCH (r)-[:INVESTIGATES]->(i:Incident)
RETURN r {.*} AS run, i.id AS incident, collect(s {.*}) AS steps
"""

STEP_TOUCHED = """
MATCH (r:AgentRun {id: $run})-[:HAS_STEP]->(s:Step)-[:TOUCHED]->(n)
RETURN s.n AS step, labels(n)[0] AS label,
       coalesce(n.name, n.id, n.handle, toString(n.number)) AS key
ORDER BY step
"""

RESUMABLE = """
MATCH (r:AgentRun)
WHERE r.status = 'paused' AND r.resume_at <= $now
RETURN r.id AS id, r.agent AS agent
ORDER BY r.resume_at
"""

CREATE_TASK = """
MATCH (i:Incident {id: $incident})
CREATE (t:Task {id: $id, type: $type, status: 'open', incident: $incident, created_at: $now,
                created_by: $created_by, payload: $payload})
CREATE (t)-[:FOR]->(i)
RETURN t.id AS id
"""

# One query = one atomic step in FalkorDB, so two workers can never claim the same task.
CLAIM_TASK = """
MATCH (t:Task {status: 'open'})
WHERE t.type IN $types
WITH t ORDER BY t.created_at LIMIT 1
SET t.status = 'claimed', t.claimed_by = $worker, t.claimed_at = $now
RETURN t.id AS id, t.type AS type, t.incident AS incident, t.payload AS payload
"""

FINISH_TASK = "MATCH (t:Task {id: $id}) SET t.status = $status, t.finished_at = $now"


def _now() -> int:
    return int(time.time())


def _json(value) -> str:
    text = json.dumps(value, default=str, ensure_ascii=False)
    return text if len(text) <= MAX_OUTPUT_CHARS else text[:MAX_OUTPUT_CHARS] + '..."[truncated]"'


def create_run(tenant, agent: str, goal: str, system: str, user: str,
               incident: str | None = None, task: str | None = None, session: str | None = None) -> str:
    run_id = f"RUN-{agent}-{uuid.uuid4().hex[:8]}"
    db.graph(tenant).query(CREATE_RUN, {
        "id": run_id, "agent": agent, "goal": goal, "now": _now(), "system": system, "user": user,
        "incident": incident or "", "task": task or "", "session": session,
    })
    return run_id


def add_step(tenant, run_id: str, n: int, kind: str, *, tool=None, args=None, output=None, message=None,
             tool_call_id=None, provider=None, model=None, tokens=None, latency_ms=None,
             touched: list[tuple[str, str]] | None = None) -> str:
    g = db.graph(tenant)
    step_id = f"{run_id}-{n}"
    g.query(ADD_STEP, {
        "run": run_id, "id": step_id, "n": n, "kind": kind, "tool": tool,
        "args": _json(args) if args is not None else None,
        "output": _json(output) if output is not None else None,
        "message": json.dumps(message) if message is not None else None,
        "tool_call_id": tool_call_id, "provider": provider, "model": model,
        "tokens": tokens, "latency_ms": latency_ms, "now": _now(),
    })
    if touched:
        link_touched(tenant, step_id, touched)
    return step_id


TOUCH_KEYS = {"Component": "name", "Person": "handle", "Incident": "id", "Decision": "id",
              "Document": "id", "Team": "name", "Runbook": "id", "PullRequest": "number",
              "Lesson": "id", "UserFlow": "name"}


def link_touched(tenant, step_id: str, touched: list[tuple[str, str]]) -> None:
    link_entities(tenant, "Step", step_id, "TOUCHED", touched)


def link_entities(tenant, from_label: str, from_id: str, rel: str, refs: list[tuple[str, str]]) -> None:
    """MERGE (from)-[rel]->(entity) for each (label, key). Labels/rel come from code, never from the LLM."""
    g = db.graph(tenant)
    by_label: dict[str, set] = {}
    for label, key in refs:
        by_label.setdefault(label, set()).add(key)
    for label, keys in by_label.items():
        prop = TOUCH_KEYS[label]
        g.query(f"MATCH (s:{from_label} {{id: $sid}}) UNWIND $keys AS k MATCH (n:{label} {{{prop}: k}}) "
                f"MERGE (s)-[:{rel}]->(n)", {"sid": from_id, "keys": sorted(keys, key=str)})


def set_status(tenant, run_id: str, status: str, *, resume_at=None, error=None, result=None, provider=None):
    db.graph(tenant).query(SET_STATUS, {
        "id": run_id, "status": status, "now": _now(), "resume_at": resume_at, "error": error,
        "result": json.dumps(result, default=str) if result is not None else None, "provider": provider,
    })


def load_run(tenant, run_id: str) -> dict | None:
    rows = db.rows(db.graph(tenant).ro_query(LOAD_RUN, {"id": run_id}))
    if not rows or not rows[0]["run"]:
        return None
    row = rows[0]
    run = dict(row["run"])
    run["incident"] = row["incident"]
    run["steps"] = sorted((dict(s) for s in row["steps"] if s.get("id")), key=lambda s: s["n"])
    if run.get("result"):
        run["result"] = json.loads(run["result"])
    return run


def touched_by_step(tenant, run_id: str) -> list[dict]:
    return db.rows(db.graph(tenant).ro_query(STEP_TOUCHED, {"run": run_id}))


def resumable_runs(tenant, now: int | None = None) -> list[dict]:
    return db.rows(db.graph(tenant).ro_query(RESUMABLE, {"now": now or _now()}))


def create_task(tenant, task_type: str, incident: str, created_by: str, payload: dict | None = None) -> str:
    task_id = f"TASK-{task_type}-{uuid.uuid4().hex[:8]}"
    db.graph(tenant).query(CREATE_TASK, {"id": task_id, "type": task_type, "incident": incident,
                                         "now": _now(), "created_by": created_by,
                                         "payload": json.dumps(payload or {})})
    return task_id


def claim_task(tenant, types: list[str], worker: str) -> dict | None:
    rows = db.rows(db.graph(tenant).query(CLAIM_TASK, {"types": types, "worker": worker, "now": _now()}))
    if not rows:
        return None
    task = rows[0]
    task["payload"] = json.loads(task["payload"] or "{}")
    return task


def finish_task(tenant, task_id: str, status: str = "done") -> None:
    db.graph(tenant).query(FINISH_TASK, {"id": task_id, "status": status, "now": _now()})


# --- Finding which graph entities a tool result mentions --------------------------------

@lru_cache(maxsize=16)
def _vocabulary(tenant: str) -> dict:
    g = db.graph(tenant)
    return {
        "Component": {r["k"] for r in db.rows(g.ro_query("MATCH (c:Component) RETURN c.name AS k"))},
        "Person": {r["k"] for r in db.rows(g.ro_query("MATCH (p:Person) RETURN p.handle AS k"))},
        "Team": {r["k"] for r in db.rows(g.ro_query("MATCH (t:Team) RETURN t.name AS k"))},
        "UserFlow": {r["k"] for r in db.rows(g.ro_query("MATCH (f:UserFlow) RETURN f.name AS k"))},
    }


_ID_PATTERNS = [
    (re.compile(r"^INC-\d+$"), "Incident"),
    (re.compile(r"^ADR-\d+$"), "Decision"),
    (re.compile(r"^(ADR-\d+-doc|PM-INC-\d+)$"), "Document"),
    (re.compile(r"^RB-[\w-]+$"), "Runbook"),
    (re.compile(r"^LES-[\w-]+$"), "Lesson"),
]


def entities_in(tenant: str, value) -> list[tuple[str, str]]:
    """Every known graph entity mentioned anywhere in a (nested) tool result."""
    vocab = _vocabulary(tenant)
    found: set[tuple[str, str]] = set()

    def walk(v):
        if isinstance(v, dict):
            for k, x in v.items():
                if k == "pr" and isinstance(x, int):
                    found.add(("PullRequest", x))
                walk(x)
        elif isinstance(v, (list, tuple)):
            for x in v:
                walk(x)
        elif isinstance(v, str):
            for label in ("Component", "Person", "Team"):
                if v in vocab[label]:
                    found.add((label, v))
            for pattern, label in _ID_PATTERNS:
                if pattern.match(v):
                    found.add((label, v))

    walk(value)
    return sorted(found, key=lambda t: (t[0], str(t[1])))


_PR_IN_TEXT = re.compile(r"\bPR\s*#?(\d{3,6})\b", re.IGNORECASE)
_ID_IN_TEXT = re.compile(r"\b(INC-\d+|ADR-\d+|RB-[\w-]+|LES-INC-\d+)\b")


def entities_in_text(tenant: str, text: str) -> list[tuple[str, str]]:
    """Graph entities cited in free text written by the model (component names, PR 1082, INC-101, ...)."""
    vocab = _vocabulary(tenant)
    found: set = set()
    for label in ("Component", "Person"):
        for key in vocab[label]:
            if re.search(rf"(?<![\w-]){re.escape(key)}(?![\w-])", text):
                found.add((label, key))
    found.update(("PullRequest", int(n)) for n in _PR_IN_TEXT.findall(text))
    for ident in _ID_IN_TEXT.findall(text):
        found.update(e for e in entities_in(tenant, ident))
    return sorted(found, key=lambda t: (t[0], str(t[1])))

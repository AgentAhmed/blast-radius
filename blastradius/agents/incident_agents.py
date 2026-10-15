"""The on-call team: three agents that hand work to each other through Task nodes in the graph.

  Triage        alerts -> Incident (+ severity from user flows at risk) -> Task 'investigate'
  Investigator  LLM + graph tools -> Finding (root cause, evidence) -> page owner, open issue
  Scribe        on resolution -> postmortem Document, Lesson, embedding, SIMILAR_TO links (memory)

Triage is deterministic on purpose: grouping alerts and scoring severity needs no judgement, so it costs
no LLM calls. The LLM is used where judgement is needed: weighing evidence and writing for humans.
"""
import json
import time
import uuid

from blastradius import actions, db, embeddings, incidents, memory, queries
from blastradius.agents import runtime, store
from blastradius.agents.runtime import AgentSpec
from blastradius.agents.tools import FINAL_ANSWER, SUBMIT_FINDINGS, WRITE_POSTMORTEM, ToolError, toolset

# ---------------------------------------------------------------------------------------------
# Triage
# ---------------------------------------------------------------------------------------------


def triage(tenant: str) -> dict | None:
    """Group firing alerts into an incident and hand it to the investigator."""
    alerts = incidents.ungrouped_alerts(tenant)
    if not alerts:
        return None
    inc = incidents.open_incident(alerts, tenant)
    run_id = store.create_run(tenant, "triage", f"Triage alerts into {inc['id']}",
                              system="deterministic", user=json.dumps([a["id"] for a in alerts]),
                              incident=inc["id"])
    store.add_step(tenant, run_id, 1, "tool", tool="group_alerts",
                   args={"alerts": len(alerts)}, output=inc,
                   touched=[("Component", c) for c in inc["components"]] + [("Incident", inc["id"])])
    task_id = store.create_task(tenant, "investigate", inc["id"], created_by=run_id)
    store.add_step(tenant, run_id, 2, "tool", tool="handoff", args={"to": "investigator"},
                   output={"task": task_id}, touched=[("Incident", inc["id"])])
    store.set_status(tenant, run_id, "done", result={"incident": inc["id"], "task": task_id})
    return {**inc, "task": task_id, "run_id": run_id}


# ---------------------------------------------------------------------------------------------
# Investigator
# ---------------------------------------------------------------------------------------------

INVESTIGATOR_SYSTEM = """You are the on-call investigator for a microservice system. The system, its owners,
change history and past incidents are in a graph database that you query through tools. You never guess:
every claim must come from a tool result.

Work efficiently (you have a small step budget). A good plan:
1. root_cause_candidates with all alerting components.
2. recent_changes and similar_incidents for the top candidate(s). Call several tools in one turn when you can.
3. past_fixes for the most likely root cause if a fix is not yet clear.
4. submit_findings with the root cause, your confidence, short evidence and a concrete recommended action.

Reading root_cause_candidates: 'covers' = how many alerting components depend on it; a candidate that is
alerting, has no alerting dependencies of its own and changed recently is the strongest suspect. A
component that does not alert itself can still be the cause (e.g. a Kafka topic or datastore).

Separate facts from hypotheses: past incidents show what happened before, not what is happening now.
Say "similar to INC-x, which was ..." rather than stating an old cause as the current one."""

INVESTIGATOR_TOOLS = ["root_cause_candidates", "recent_changes", "similar_incidents", "past_fixes",
                      "blast_radius", "path_to_user", "component_overview", "who_to_page", "criticality"]


def _validate_findings(tenant, result):
    if result.get("root_cause") not in queries.component_names(tenant):
        raise ToolError(f"root_cause must be a component name, got '{result.get('root_cause')}'")
    try:
        result["confidence"] = max(0.0, min(1.0, float(result.get("confidence", 0))))
    except (TypeError, ValueError):
        raise ToolError("confidence must be a number between 0 and 1")


RECORD_FINDING = """
MATCH (i:Incident {id: $incident}), (rc:Component {name: $root})
CREATE (f:Finding {id: $id, root_cause: $root, confidence: $confidence, summary: $summary,
                   evidence: $evidence, suspect_change: $suspect, recommended_action: $action,
                   created_at: $now, run: $run})
CREATE (i)-[:HAS_FINDING]->(f)
CREATE (f)-[:BLAMES]->(rc)
WITH f
OPTIONAL MATCH (s:Step {run: $run})-[:TOUCHED]->(n)
WHERE NOT n:Incident
WITH f, collect(DISTINCT n) AS evidence_nodes
FOREACH (n IN evidence_nodes | MERGE (f)-[:SUPPORTED_BY]->(n))
WITH f
MATCH (last:Step {id: $last_step})
MERGE (last)-[:PRODUCED]->(f)
"""


def _after_investigation(tenant, run, result):
    incident_id = run["incident"]
    last_step = run["steps"][-1]["id"]
    finding_id = f"FND-{incident_id}-{uuid.uuid4().hex[:4]}"
    db.graph(tenant).query(RECORD_FINDING, {
        "incident": incident_id, "root": result["root_cause"], "id": finding_id,
        "confidence": result["confidence"], "summary": result["summary"],
        "evidence": result.get("evidence") or [], "suspect": result.get("suspect_change") or "",
        "action": result["recommended_action"], "now": int(time.time()), "run": run["id"], "last_step": last_step,
    })
    db.graph(tenant).query("MATCH (i:Incident {id: $id}) SET i.status = 'identified'", {"id": incident_id})

    # Act: page the on-call owner of the root cause, open a tracking issue.
    page_to = queries.who_to_page(result["root_cause"], tenant)
    if page_to and page_to[0]["on_call"]:
        actions.page(tenant, incident_id, page_to[0]["on_call"],
                     f"On call for {page_to[0]['team']}, owner of {result['root_cause']}")
    evidence = "\n".join(f"- {e}" for e in result.get("evidence") or [])
    actions.github_issue(
        tenant, incident_id, f"[{incident_id}] Root cause: {result['root_cause']}",
        f"{result['summary']}\n\n**Evidence (from the graph)**\n{evidence}\n\n"
        f"**Recommended action:** {result['recommended_action']}\n\n"
        f"Confidence: {result['confidence']:.0%} - investigated by Blast Radius run `{run['id']}`")


INVESTIGATOR = AgentSpec(
    name="investigator",
    tools=toolset(INVESTIGATOR_TOOLS, SUBMIT_FINDINGS),
    max_llm_calls=7,
    validate=_validate_findings,
    on_finish=_after_investigation,
)


def investigate(tenant: str, incident_id: str, task_id: str | None = None) -> dict:
    inc = incidents.get_incident(incident_id, tenant)
    alerting = sorted({a["component"] for a in inc["alerts"]})
    alert_lines = "\n".join(f"- {a['component']}: {a['signal']} = {a['value']} ({a['severity']})"
                            for a in sorted(inc["alerts"], key=lambda a: a["fired_at"]))
    user = (f"Incident {incident_id} ({inc['incident']['severity']}): {inc['incident']['title']}\n"
            f"{inc['incident']['summary']}\n\nFiring alerts:\n{alert_lines}\n\n"
            f"Alerting components: {json.dumps(alerting)}\nFind the root cause.")
    db.graph(tenant).query("MATCH (i:Incident {id: $id}) SET i.status = 'investigating'", {"id": incident_id})
    return runtime.start(tenant, INVESTIGATOR, f"Find root cause of {incident_id}", INVESTIGATOR_SYSTEM, user,
                         incident=incident_id, task=task_id)


# ---------------------------------------------------------------------------------------------
# Scribe
# ---------------------------------------------------------------------------------------------

SCRIBE_SYSTEM = """You are the incident scribe. Write a short, factual postmortem for the resolved incident
from the facts given, and one reusable lesson for the next on-call engineer. Then call write_postmortem."""

RESOLVE = """
MATCH (i:Incident {id: $id}), (rc:Component {name: $root})
SET i.status = 'resolved', i.resolved_at = $now, i.summary = $summary
MERGE (i)-[:ROOT_CAUSE]->(rc)
MERGE (a:Action {id: $id + '-fix'})
SET a.kind = $kind, a.detail = $fix, a.at = $now
MERGE (i)-[:RESOLVED_BY]->(a)
MERGE (d:Document {id: 'PM-' + $id})
SET d.kind = 'postmortem', d.title = 'Postmortem: ' + i.title, d.body = $summary + ' Resolution: ' + $fix,
    d.embedding = vecf32($doc_vec)
MERGE (d)-[:POSTMORTEM_OF]->(i)
MERGE (d)-[:ABOUT]->(rc)
MERGE (l:Lesson {id: 'LES-' + $id})
SET l.text = $lesson, l.incident = $id, l.at = $now
MERGE (l)-[:APPLIES_TO]->(rc)
MERGE (l)-[:LEARNED_FROM]->(i)
SET i.embedding = vecf32($inc_vec)
"""


def _after_postmortem(tenant, run, result):
    task_payload = json.loads(run["user_prompt"].split("\n---\n")[-1])
    incident_id, root = run["incident"], task_payload["root_cause"]
    inc = incidents.get_incident(incident_id, tenant)
    g = db.graph(tenant)
    g.query(RESOLVE, {
        "id": incident_id, "root": root, "now": int(time.time()), "summary": result["summary"],
        "kind": task_payload["fix_kind"], "fix": task_payload["fix"], "lesson": result["lesson"],
        "doc_vec": embeddings.embed_one(f"Postmortem: {inc['incident']['title']}. {result['summary']}"),
        "inc_vec": embeddings.embed_one(embeddings.incident_text(inc["incident"]["title"], result["summary"],
                                                                 [root] + inc["impacted"])),
    })
    memory.link_similar(g, incident_id)


SCRIBE = AgentSpec(name="scribe", tools={WRITE_POSTMORTEM.name: WRITE_POSTMORTEM}, max_llm_calls=2,
                   on_finish=_after_postmortem, temperature=0.3)


def resolve(tenant: str, incident_id: str, fix: str, fix_kind: str = "fix", root_cause: str | None = None) -> str:
    """A human marks the incident resolved; this hands it to the scribe."""
    inc = incidents.get_incident(incident_id, tenant)
    root = root_cause or (inc["finding"] or {}).get("root_cause") or inc["root_cause"]
    if root not in queries.component_names(tenant):
        raise ValueError("root_cause is required when the incident has no finding")
    db.graph(tenant).query("MATCH (i:Incident {id: $id}) SET i.status = 'resolving'", {"id": incident_id})
    return store.create_task(tenant, "record", incident_id, created_by="human",
                             payload={"fix": fix, "fix_kind": fix_kind, "root_cause": root})


def write_memory(tenant: str, incident_id: str, payload: dict, task_id: str | None = None) -> dict:
    inc = incidents.get_incident(incident_id, tenant)
    facts = {
        "incident": inc["incident"], "alerts": [f"{a['component']}: {a['signal']}" for a in inc["alerts"]],
        "impacted": inc["impacted"], "finding": inc["finding"], "paged": inc["paged"],
        "root_cause": payload["root_cause"], "fix": payload["fix"],
    }
    user = f"Facts:\n{json.dumps(facts, default=str, indent=1)}\n---\n{json.dumps(payload)}"
    return runtime.start(tenant, SCRIBE, f"Record memory of {incident_id}", SCRIBE_SYSTEM, user,
                         incident=incident_id, task=task_id)


# ---------------------------------------------------------------------------------------------
# Company brain: answers questions about the system and the organisation
# ---------------------------------------------------------------------------------------------

BRAIN_SYSTEM = """You answer questions about a company's software system and organisation: who owns and
knows what, why things are built the way they are, what breaks if something fails, and what happened in
past incidents. All knowledge is in a graph you query through tools. Base every statement on tool results,
cite the ids/names you used in `sources`, and say so if the graph does not contain the answer.
Call several tools in one turn when useful. Finish by calling final_answer."""

BRAIN_TOOLS = ["component_overview", "who_to_page", "experts", "decisions_for", "search_docs", "person_profile",
               "bus_factor", "blast_radius", "path_to_user", "criticality", "similar_incidents", "past_fixes",
               "incident_details", "list_incidents", "recent_changes"]

BRAIN = AgentSpec(name="brain", tools=toolset(BRAIN_TOOLS, FINAL_ANSWER), max_llm_calls=6,
                  accept_text_answer=True)

SESSION_HISTORY = """
MATCH (r:AgentRun {agent: 'brain', session: $session, status: 'done'})
RETURN r.goal AS question, r.result AS result
ORDER BY r.started_at DESC LIMIT 3
"""


def ask(tenant: str, question: str, session: str | None = None) -> dict:
    """Answer a question; previous answers in the same session are given as context (conversation memory)."""
    history = ""
    if session:
        turns = db.rows(db.graph(tenant).ro_query(SESSION_HISTORY, {"session": session}))
        if turns:
            lines = [f"Q: {t['question']}\nA: {json.loads(t['result'])['answer']}" for t in reversed(turns)]
            history = "Earlier in this conversation:\n" + "\n\n".join(lines) + "\n\n"
    return runtime.start(tenant, BRAIN, question, BRAIN_SYSTEM, f"{history}Question: {question}", session=session)


AGENTS = {"investigator": INVESTIGATOR, "scribe": SCRIBE, "brain": BRAIN}

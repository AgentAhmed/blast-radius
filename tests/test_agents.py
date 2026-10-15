"""Agent runtime, coordination and memory, with a scripted fake LLM (no network). Needs FalkorDB."""
import json
from types import SimpleNamespace

import pytest

from blastradius import db, incidents
from blastradius.agents import incident_agents, runtime, store
from blastradius.ingest import ingest
from blastradius.llm import AllProvidersExhausted, LLMResult

TENANT = "test_agents"


@pytest.fixture(scope="module", autouse=True)
def graph():
    ingest(TENANT, reset=True)
    yield
    db.graph(TENANT).delete()


def call(name, **args):
    return SimpleNamespace(id=f"call_{name}_{len(json.dumps(args))}",
                           function=SimpleNamespace(name=name, arguments=json.dumps(args)))


def reply(*calls, content=""):
    return SimpleNamespace(content=content, tool_calls=list(calls) or None)


class ScriptedLLM:
    """Returns the scripted replies in order; an exception in the script is raised instead."""

    def __init__(self, script):
        self.script, self.calls = list(script), 0

    def chat(self, messages, tools=None, temperature=0.2, max_tokens=None):
        self.calls += 1
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return LLMResult(message=item, provider="fake", model="scripted", usage={"total_tokens": 10}, latency_ms=1)


FINDINGS = dict(root_cause="payment", confidence=0.9, summary="payment fails", evidence=["payment covers 4"],
                recommended_action="roll back payment")


def new_payment_incident():
    incidents.simulate_fault("paymentFailure", TENANT)
    return incident_agents.triage(TENANT)


def tool_steps(run_id):
    return [s["tool"] for s in store.load_run(TENANT, run_id)["steps"] if s["kind"] == "tool"]


def test_simulator_and_triage_open_incident_with_task():
    inc = new_payment_incident()
    assert inc["severity"] == "SEV1"                    # checkout flow at risk
    assert set(inc["components"]) == {"payment", "checkout", "frontend", "frontend-proxy"}
    task = store.claim_task(TENANT, ["investigate"], "w1")
    assert task["incident"] == inc["id"]
    store.finish_task(TENANT, task["id"])


def test_topic_fault_alerts_consumers_not_the_topic():
    incidents.simulate_fault("kafkaQueueProblems", TENANT)
    alerts = incidents.ungrouped_alerts(TENANT)
    assert {"accounting", "fraud-detection", "checkout"} <= {a["component"] for a in alerts}
    assert "orders" not in {a["component"] for a in alerts}
    inc = incident_agents.triage(TENANT)
    store.finish_task(TENANT, store.claim_task(TENANT, ["investigate"], "w")["id"])
    assert inc["id"].startswith("INC-")


def test_task_can_only_be_claimed_once():
    inc = new_payment_incident()
    first = store.claim_task(TENANT, ["investigate"], "w1")
    second = store.claim_task(TENANT, ["investigate"], "w2")
    assert first["incident"] == inc["id"] and second is None
    store.finish_task(TENANT, first["id"])


def test_investigation_records_finding_page_and_evidence_trail():
    inc = new_payment_incident()
    llm = ScriptedLLM([
        reply(call("root_cause_candidates", alerting_components=inc["components"])),
        reply(call("recent_changes", component="payment"), call("past_fixes", component="payment")),
        reply(call("submit_findings", **FINDINGS)),
    ])
    out = runtime.start(TENANT, incident_agents.INVESTIGATOR, "t", "sys", "user", incident=inc["id"], gw=llm)
    assert out["status"] == "done" and out["result"]["root_cause"] == "payment"
    assert tool_steps(out["run_id"]) == ["root_cause_candidates", "recent_changes", "past_fixes", "submit_findings"]

    detail = incidents.get_incident(inc["id"], TENANT)
    assert detail["finding"]["root_cause"] == "payment"
    assert detail["paged"] and detail["paged"][0]["handle"] in {"priya.n", "jonas.b", "chen.w"}
    g = db.graph(TENANT)
    chain = db.rows(g.ro_query("MATCH (:AgentRun {id: $id})-[:HAS_STEP]->(s:Step) WHERE NOT ()-[:NEXT]->(s) "
                               "MATCH p = (s)-[:NEXT*0..]->(e) RETURN max(length(p)) AS len", {"id": out["run_id"]}))
    assert chain[0]["len"] == 6                        # 7 steps linked in one chain
    evidence = db.rows(g.ro_query("MATCH (f:Finding {id: $id})-[:SUPPORTED_BY]->(n) RETURN collect(n.name) AS names",
                                  {"id": detail["finding"]["id"]}))[0]["names"]
    assert "payment" in evidence
    store.finish_task(TENANT, store.claim_task(TENANT, ["investigate"], "w")["id"])


def test_invalid_findings_are_sent_back_to_the_model():
    inc = new_payment_incident()
    llm = ScriptedLLM([
        reply(call("submit_findings", **{**FINDINGS, "root_cause": "the database"})),
        reply(call("submit_findings", **FINDINGS)),
    ])
    out = runtime.start(TENANT, incident_agents.INVESTIGATOR, "t", "sys", "user", incident=inc["id"], gw=llm)
    steps = store.load_run(TENANT, out["run_id"])["steps"]
    assert "must be a component name" in steps[1]["output"]
    assert out["status"] == "done"
    store.finish_task(TENANT, store.claim_task(TENANT, ["investigate"], "w")["id"])


def test_rate_limited_run_pauses_and_resumes_without_repeating_work():
    inc = new_payment_incident()
    first = ScriptedLLM([
        reply(call("root_cause_candidates", alerting_components=inc["components"])),
        AllProvidersExhausted(resume_at=1, attempts=[{"provider": "fake", "error": "rate limited"}]),
    ])
    out = runtime.start(TENANT, incident_agents.INVESTIGATOR, "t", "sys", "user", incident=inc["id"], gw=first)
    assert out["status"] == "paused"
    assert {"id": out["run_id"], "agent": "investigator"} in store.resumable_runs(TENANT)

    second = ScriptedLLM([reply(call("submit_findings", **FINDINGS))])
    done = runtime.execute(TENANT, incident_agents.INVESTIGATOR, out["run_id"], gw=second)
    assert done["status"] == "done" and second.calls == 1
    assert tool_steps(out["run_id"]) == ["root_cause_candidates", "submit_findings"]
    store.finish_task(TENANT, store.claim_task(TENANT, ["investigate"], "w")["id"])


def test_crash_after_llm_reply_runs_the_pending_tool_on_restart():
    inc = new_payment_incident()
    run_id = store.create_run(TENANT, "investigator", "t", "sys", "user", incident=inc["id"])
    pending = {"role": "assistant", "content": "", "tool_calls": [
        {"id": "c1", "type": "function", "function": {"name": "blast_radius", "arguments": '{"component": "payment"}'}}]}
    store.add_step(TENANT, run_id, 1, "llm", message=pending, provider="fake", model="m")   # then "crash"

    llm = ScriptedLLM([reply(call("submit_findings", **FINDINGS))])
    out = runtime.execute(TENANT, incident_agents.INVESTIGATOR, run_id, gw=llm)
    assert out["status"] == "done"
    assert tool_steps(run_id) == ["blast_radius", "submit_findings"]
    store.finish_task(TENANT, store.claim_task(TENANT, ["investigate"], "w")["id"])


def test_resolution_writes_memory_that_next_investigation_can_recall():
    inc = new_payment_incident()
    runtime.start(TENANT, incident_agents.INVESTIGATOR, "t", "sys", "user", incident=inc["id"],
                  gw=ScriptedLLM([reply(call("submit_findings", **FINDINGS))]))
    store.finish_task(TENANT, store.claim_task(TENANT, ["investigate"], "w")["id"])

    incident_agents.resolve(TENANT, inc["id"], "Rolled back the payment v3 SDK release", "rollback")
    task = store.claim_task(TENANT, ["record"], "w")
    payload = task["payload"]
    user = f"facts\n---\n{json.dumps(payload)}"
    out = runtime.start(TENANT, incident_agents.SCRIBE, "t", "sys", user, incident=inc["id"], task=task["id"],
                        gw=ScriptedLLM([reply(call("write_postmortem", summary="Payment SDK upgrade broke payments.",
                                                   lesson="Canary payment client upgrades."))]))
    assert out["status"] == "done"

    detail = incidents.get_incident(inc["id"], TENANT)
    assert detail["incident"]["status"] == "resolved" and detail["root_cause"] == "payment"
    from blastradius import queries
    fixes = queries.past_fixes("payment", TENANT)
    assert any(lesson["text"] == "Canary payment client upgrades." for lesson in fixes["lessons"])
    similar = queries.similar_incidents("payment errors breaking checkout", ["payment"], TENANT)
    assert inc["id"] in [s["incident"] for s in similar]


def test_humanize_times():
    now = 1_000_000_000 + 10 * 86400
    out = runtime.humanize_times({"merged_at": now - 7200, "on_call_until": now + 3 * 86400, "pr": 1082}, now)
    assert out == {"merged_at": "2h ago", "on_call_until": "in 3d", "pr": 1082}


def test_final_answer_called_with_other_tools_runs_last():
    llm = ScriptedLLM([reply(call("final_answer", answer="a", sources=[]),
                             call("decisions_for", component="checkout"))])
    out = runtime.start(TENANT, incident_agents.BRAIN, "q", "sys", "user", gw=llm)
    assert tool_steps(out["run_id"]) == ["decisions_for", "final_answer"]


def test_entities_cited_in_text():
    pr = db.rows(db.graph(TENANT).ro_query(
        "MATCH (pr:PullRequest {title: 'Switch payment gateway client to v3 SDK'}) RETURN pr.number AS n"))[0]["n"]
    refs = store.entities_in_text(TENANT, f"payment broke after PR {pr}; like INC-101. Not payments-team.")
    assert ("Component", "payment") in refs and ("PullRequest", pr) in refs and ("Incident", "INC-101") in refs
    assert ("Component", "checkout") not in refs

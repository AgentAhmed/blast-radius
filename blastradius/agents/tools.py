"""Graph tools the agents can call. Each wraps a fixed, tested Cypher query from queries.py.

The LLM never writes Cypher: it picks a tool and its arguments, we validate them, run the query and
return a compact result. The same tools are exposed over MCP (blastradius/mcp_server.py).
"""
from dataclasses import dataclass
from typing import Callable

from blastradius import incidents, queries


class ToolError(Exception):
    """Bad arguments from the model; the message goes back to it so it can correct itself."""


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    fn: Callable          # fn(tenant, **args) -> result
    terminal: bool = False  # calling it ends the agent run with its arguments as the result

    def schema(self) -> dict:
        return {"type": "function",
                "function": {"name": self.name, "description": self.description, "parameters": self.parameters}}


def _obj(props: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": props, "required": required, "additionalProperties": False}


COMPONENT = {"type": "string", "description": "Component name, e.g. 'payment', 'valkey-cart', 'orders'"}


def _check_component(tenant, name: str) -> str:
    names = queries.component_names(tenant)
    if name not in names:
        raise ToolError(f"Unknown component '{name}'. Valid names: {', '.join(names)}")
    return name


def _blast_radius(tenant, component):
    r = queries.blast_radius(_check_component(tenant, component), tenant)
    return {"component": component,
            "dependents": [{k: d[k] for k in ("component", "hops", "tier")} for d in r["dependents"]],
            "flows_at_risk": [f["flow"] for f in r["flows_at_risk"]]}


def _root_cause(tenant, alerting_components, window_hours=24):
    for c in alerting_components:
        _check_component(tenant, c)
    return queries.root_cause_candidates(alerting_components, tenant, window_hours=window_hours, limit=5)


def _recent_changes(tenant, component, window_hours=24):
    rows = queries.recent_changes(_check_component(tenant, component), tenant, window_hours=window_hours)
    return rows[:10]


def _who_to_page(tenant, component):
    return queries.who_to_page(_check_component(tenant, component), tenant)


def _experts(tenant, component):
    return queries.experts(_check_component(tenant, component), tenant, limit=5)


def _similar_incidents(tenant, description, components, exclude_incident=""):
    return queries.similar_incidents(description, components, tenant, exclude=exclude_incident, limit=3)


def _past_fixes(tenant, component):
    return queries.past_fixes(_check_component(tenant, component), tenant)


def _path_to_user(tenant, component):
    return queries.path_to_user(_check_component(tenant, component), tenant)


def _criticality(tenant, limit=8):
    return queries.criticality(tenant, limit=limit)


def _component_overview(tenant, component):
    return queries.component_overview(_check_component(tenant, component), tenant)


def _decisions_for(tenant, component):
    return queries.decisions_for(_check_component(tenant, component), tenant)


def _search_docs(tenant, query):
    return [{**d, "body": d["body"][:600]} for d in queries.search_docs(query, tenant, limit=3)]


def _person_profile(tenant, handle):
    r = queries.person_profile(handle, tenant)
    if r is None:
        raise ToolError(f"Unknown person handle '{handle}'")
    return r


def _bus_factor(tenant):
    return queries.bus_factor(tenant, max_people=1)


def _incident_details(tenant, incident_id):
    r = incidents.get_incident(incident_id, tenant)
    if r is None:
        raise ToolError(f"Unknown incident '{incident_id}'")
    return r


def _list_incidents(tenant, limit=10):
    return incidents.list_incidents(tenant, limit=limit)


ALL: dict[str, Tool] = {t.name: t for t in [
    Tool("root_cause_candidates",
         "Rank likely root causes for a set of alerting components by following dependencies in the graph. "
         "Use this first in an investigation.",
         _obj({"alerting_components": {"type": "array", "items": {"type": "string"}},
               "window_hours": {"type": "integer", "description": "How far back to count recent changes (default 24)"}},
              ["alerting_components"]),
         _root_cause),
    Tool("blast_radius", "Everything that transitively depends on a component, and the user flows at risk.",
         _obj({"component": COMPONENT}, ["component"]), _blast_radius),
    Tool("recent_changes",
         "Pull requests and releases in a component and its dependencies (up to 2 hops) within a time window.",
         _obj({"component": COMPONENT, "window_hours": {"type": "integer"}}, ["component"]), _recent_changes),
    Tool("similar_incidents",
         "Past resolved incidents similar to the current one (semantic + same components), with their fixes.",
         _obj({"description": {"type": "string", "description": "Short description of the current symptoms"},
               "components": {"type": "array", "items": {"type": "string"}},
               "exclude_incident": {"type": "string", "description": "Current incident id, to exclude it"}},
              ["description", "components"]),
         _similar_incidents),
    Tool("past_fixes", "Runbooks, lessons learned and past incident fixes for a component.",
         _obj({"component": COMPONENT}, ["component"]), _past_fixes),
    Tool("path_to_user", "Shortest dependency path from the user entry point (frontend-proxy) to a component.",
         _obj({"component": COMPONENT}, ["component"]), _path_to_user),
    Tool("criticality", "Most critical components by PageRank over the dependency graph.",
         _obj({"limit": {"type": "integer"}}, []), _criticality),
    Tool("component_overview", "Owner, tier, dependencies, dependents, user flows and incident count of a component.",
         _obj({"component": COMPONENT}, ["component"]), _component_overview),
    Tool("who_to_page", "Owner team and the person on call right now for a component.",
         _obj({"component": COMPONENT}, ["component"]), _who_to_page),
    Tool("experts", "People who know a component best, from their change history.",
         _obj({"component": COMPONENT}, ["component"]), _experts),
    Tool("decisions_for", "Design decisions (ADRs) that affect a component, who made them and the source document.",
         _obj({"component": COMPONENT}, ["component"]), _decisions_for),
    Tool("search_docs", "Semantic search over company documents: postmortems and design decisions.",
         _obj({"query": {"type": "string"}}, ["query"]), _search_docs),
    Tool("person_profile", "What a person's team owns, what they worked on and incidents they handled.",
         _obj({"handle": {"type": "string", "description": "Person handle, e.g. 'chen.w'"}}, ["handle"]),
         _person_profile),
    Tool("bus_factor", "Services that only one person has ever changed (knowledge risk).",
         _obj({}, []), _bus_factor),
    Tool("incident_details", "Alerts, impact, findings and actions of one incident.",
         _obj({"incident_id": {"type": "string"}}, ["incident_id"]), _incident_details),
    Tool("list_incidents", "Most recent incidents with status and root cause.",
         _obj({"limit": {"type": "integer"}}, []), _list_incidents),
]}

# Terminal tools: the agent's structured final output.
SUBMIT_FINDINGS = Tool(
    "submit_findings",
    "Submit the investigation result. Call exactly once, when you are confident or out of useful steps.",
    _obj({
        "root_cause": {"type": "string", "description": "Component name of the most likely root cause"},
        "confidence": {"type": "number", "description": "0.0 to 1.0"},
        "summary": {"type": "string", "description": "2-4 sentences: what failed, why, and the impact"},
        "evidence": {"type": "array", "items": {"type": "string"},
                     "description": "Short facts from tool results that support the conclusion"},
        "suspect_change": {"type": "string", "description": "PR number/title that likely caused it, or empty"},
        "recommended_action": {"type": "string", "description": "Concrete next step for the on-call engineer"},
    }, ["root_cause", "confidence", "summary", "evidence", "recommended_action"]),
    lambda tenant, **kw: kw, terminal=True)

FINAL_ANSWER = Tool(
    "final_answer",
    "Give the final answer to the user's question. Call exactly once.",
    _obj({"answer": {"type": "string", "description": "Clear answer in a few sentences or bullet points"},
          "sources": {"type": "array", "items": {"type": "string"},
                      "description": "Ids/names of graph entities the answer is based on (e.g. ADR-001, INC-101, payment)"}},
         ["answer", "sources"]),
    lambda tenant, **kw: kw, terminal=True)

WRITE_POSTMORTEM = Tool(
    "write_postmortem",
    "Write the postmortem for the resolved incident. Call exactly once.",
    _obj({"summary": {"type": "string", "description": "What happened, impact, root cause, how it was fixed"},
          "lesson": {"type": "string", "description": "One reusable lesson for the next on-call engineer"}},
         ["summary", "lesson"]),
    lambda tenant, **kw: kw, terminal=True)


def toolset(names: list[str], terminal: Tool) -> dict[str, Tool]:
    tools = {n: ALL[n] for n in names}
    tools[terminal.name] = terminal
    return tools

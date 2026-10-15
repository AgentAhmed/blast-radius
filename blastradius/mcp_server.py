"""MCP server: the same graph tools the agents use, for any MCP client (Claude Desktop, Cursor, VS Code, ...).

    python -m blastradius.mcp_server            # stdio transport

Client config example (claude_desktop_config.json):
    {"mcpServers": {"blast-radius": {"command": "<repo>/blastradius-venv/Scripts/python.exe",
                                     "args": ["-m", "blastradius.mcp_server"], "cwd": "<repo>"}}}
"""
from mcp.server.mcpserver import MCPServer

from blastradius import config
from blastradius.agents import incident_agents, tools
from blastradius.agents.runtime import humanize_times

mcp = MCPServer("blast-radius", instructions="Graph tools over a microservice system: topology, owners, changes, incidents and decisions.")


def _call(name: str, tenant: str | None, **args):
    try:
        return humanize_times(tools.ALL[name].fn(tenant or config.DEFAULT_TENANT, **args))
    except tools.ToolError as e:
        return {"error": str(e)}


@mcp.tool()
def root_cause_candidates(alerting_components: list[str], window_hours: int = 24, tenant: str | None = None):
    """Rank likely root causes for a set of alerting components by following dependencies in the graph."""
    return _call("root_cause_candidates", tenant, alerting_components=alerting_components, window_hours=window_hours)


@mcp.tool()
def blast_radius(component: str, tenant: str | None = None):
    """Everything that transitively depends on a component, and the user flows at risk."""
    return _call("blast_radius", tenant, component=component)


@mcp.tool()
def recent_changes(component: str, window_hours: int = 24, tenant: str | None = None):
    """Pull requests and releases in a component and its dependencies within a time window."""
    return _call("recent_changes", tenant, component=component, window_hours=window_hours)


@mcp.tool()
def who_to_page(component: str, tenant: str | None = None):
    """Owner team and the person on call right now for a component."""
    return _call("who_to_page", tenant, component=component)


@mcp.tool()
def experts(component: str, tenant: str | None = None):
    """People who know a component best, from their change history."""
    return _call("experts", tenant, component=component)


@mcp.tool()
def similar_incidents(description: str, components: list[str], tenant: str | None = None):
    """Past resolved incidents similar to a description (semantic + same components), with their fixes."""
    return _call("similar_incidents", tenant, description=description, components=components)


@mcp.tool()
def past_fixes(component: str, tenant: str | None = None):
    """Runbooks, lessons learned and past incident fixes for a component."""
    return _call("past_fixes", tenant, component=component)


@mcp.tool()
def path_to_user(component: str, tenant: str | None = None):
    """Shortest dependency path from the user entry point to a component."""
    return _call("path_to_user", tenant, component=component)


@mcp.tool()
def criticality(limit: int = 8, tenant: str | None = None):
    """Most critical components by PageRank over the dependency graph."""
    return _call("criticality", tenant, limit=limit)


@mcp.tool()
def decisions_for(component: str, tenant: str | None = None):
    """Design decisions (ADRs) affecting a component, who made them and the source document."""
    return _call("decisions_for", tenant, component=component)


@mcp.tool()
def search_docs(query: str, tenant: str | None = None):
    """Semantic search over postmortems and design decisions."""
    return _call("search_docs", tenant, query=query)


@mcp.tool()
def component_overview(component: str, tenant: str | None = None):
    """Owner, tier, dependencies, dependents, user flows and incident count of a component."""
    return _call("component_overview", tenant, component=component)


@mcp.tool()
def ask_company_brain(question: str, tenant: str | None = None):
    """Ask the Blast Radius company-brain agent a question; it answers from the graph with sources."""
    out = incident_agents.ask(tenant or config.DEFAULT_TENANT, question)
    return out.get("result") or {"status": out["status"]}


if __name__ == "__main__":
    mcp.run()

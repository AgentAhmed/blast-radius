"""Connect a public GitHub repository: build a tenant graph from its real topology, owners, changes and decisions.

    python -m blastradius.connectors.github open-telemetry/opentelemetry-demo [--max-prs 60] [--real-names]

API budget: 1 request for repo info, 1 for the file tree, 1-2 for the merged-PR list and 1 per PR for its
changed files. Files (compose, CODEOWNERS, ADRs) come from raw.githubusercontent.com, which does not count
against the API limit. Without GITHUB_TOKEN GitHub allows 60 API requests/hour, so --max-prs defaults lower.
Only public repositories are supported (hackathon rule 6: public data only).
"""
import argparse
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable

from blastradius import db, memory, schema
from blastradius.connectors import repo_model as rm

API = "https://api.github.com"
RAW = "https://raw.githubusercontent.com"


class ConnectorError(Exception):
    pass


def parse_repo(value: str) -> tuple[str, str]:
    m = re.search(r"(?:github\.com[/:])?([\w.-]+)/([\w.-]+?)(?:\.git)?/?$", value.strip())
    if not m:
        raise ConnectorError("Expected 'owner/repo' or a github.com URL")
    return m.group(1), m.group(2)


def tenant_for(owner: str, repo: str) -> str:
    return "gh-" + re.sub(r"[^a-z0-9-]+", "-", f"{owner}-{repo}".lower()).strip("-")[:60]


class GitHub:
    def __init__(self, token: str | None = None):
        self.token = token if token is not None else os.getenv("GITHUB_TOKEN") or None
        self.api_calls = 0

    def api(self, path: str):
        req = urllib.request.Request(f"{API}{path}", headers={
            "Accept": "application/vnd.github+json", "User-Agent": "blast-radius",
            **({"Authorization": f"Bearer {self.token}"} if self.token else {})})
        self.api_calls += 1
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code in (403, 429) and e.headers.get("x-ratelimit-remaining") == "0":
                reset = time.strftime("%H:%M", time.localtime(int(e.headers.get("x-ratelimit-reset", "0"))))
                raise ConnectorError(f"GitHub API rate limit reached (resets at {reset}). "
                                     "Set GITHUB_TOKEN for a higher limit.") from e
            if e.code == 404:
                raise ConnectorError(f"Not found or not public: {path}") from e
            raise ConnectorError(f"GitHub API error {e.code} for {path}") from e

    def raw(self, owner, repo, ref, path) -> str | None:
        url = f"{RAW}/{owner}/{repo}/{urllib.parse.quote(ref)}/{urllib.parse.quote(path)}"
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "blast-radius"}),
                                        timeout=30) as r:
                return r.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError:
            return None


def build(repo_ref: str, max_prs: int | None = None, real_names: bool = False, gh: GitHub | None = None,
          progress: Callable[[str], None] = lambda msg: None) -> dict:
    """Fetch a repository and turn it into a system model plus people, teams, PRs and decisions."""
    gh = gh or GitHub()
    owner, name = parse_repo(repo_ref)
    max_prs = max_prs or (150 if gh.token else 40)

    progress("Reading repository")
    info = gh.api(f"/repos/{owner}/{name}")
    if info.get("private"):
        raise ConnectorError("Only public repositories are supported")
    ref = info["default_branch"]
    tree = gh.api(f"/repos/{owner}/{name}/git/trees/{urllib.parse.quote(ref)}?recursive=1").get("tree", [])
    files = {t["path"] for t in tree if t["type"] == "blob"}
    dirs = {t["path"] for t in tree if t["type"] == "tree"}

    progress("Finding services and dependencies")
    teams = []
    if rm.CATALOG_PATH in files:
        model, teams = rm.from_catalog(gh.raw(owner, name, ref, rm.CATALOG_PATH))
    else:
        compose = rm.compose_files(files)
        model = rm.from_compose([gh.raw(owner, name, ref, f) or "" for f in compose]) if compose else rm.SystemModel()
    rm.add_path_guesses(model, dirs)
    if not model.components:
        raise ConnectorError("No services found: add a docker-compose.yml or a blastradius.yaml catalog "
                             "(see docs/CONNECTORS.md)")

    progress("Reading owners")
    codeowners = next((p for p in rm.CODEOWNERS_PATHS if p in files), None)
    if codeowners and not teams:
        rules = rm.parse_codeowners(gh.raw(owner, name, ref, codeowners) or "")
        teams = rm.teams_from_owners(rm.owners_by_component(model, rules), real_names)
    else:
        teams = [{**t, "members": [rm.person_id(m, real_names) for m in t["members"]]} for t in teams]

    progress("Reading design decisions")
    adr_paths = sorted(p for p in files if rm.ADR_PATH.search(p))[:12]
    decisions = [rm.parse_adr(p, gh.raw(owner, name, ref, p) or "", list(model.components), i + 1)
                 for i, p in enumerate(adr_paths)]

    progress("Reading merged pull requests")
    prs, page = [], 1
    while len(prs) < max_prs and page <= 3:
        batch = gh.api(f"/repos/{owner}/{name}/pulls?state=closed&sort=updated&direction=desc"
                       f"&per_page=100&page={page}")
        prs += [p for p in batch if p.get("merged_at") and not _is_bot(p.get("user") or {})]
        if len(batch) < 100:
            break
        page += 1
    prs = prs[:max_prs]

    changes, people = [], {}
    for i, pr in enumerate(prs, 1):
        if i % 10 == 0:
            progress(f"Mapping changed files to services ({i}/{len(prs)})")
        changed = gh.api(f"/repos/{owner}/{name}/pulls/{pr['number']}/files?per_page=100")
        comps = sorted({c for f in changed for c in rm.components_for_file(model, f["filename"])})
        if not comps:
            continue
        login = pr["user"]["login"]
        handle = rm.person_id(login, real_names)
        people[handle] = login if real_names else handle
        changes.append({"number": pr["number"], "title": pr["title"][:200], "author": handle,
                        "merged_at": rm.iso_to_epoch(pr["merged_at"]), "components": comps,
                        "url": pr["html_url"]})
    for t in teams:
        for m in t["members"]:
            people.setdefault(m, m)

    return {
        "source": {"repo": f"{owner}/{name}", "ref": ref, "url": info["html_url"],
                   "topology_from": model.topology_from, "real_names": real_names,
                   "api_calls": gh.api_calls},
        "model": model, "teams": teams, "people": people, "changes": changes, "decisions": decisions,
    }


def _is_bot(user: dict) -> bool:
    return user.get("type") == "Bot" or user.get("login", "").endswith("[bot]")


def load(tenant: str, data: dict, reset: bool = True) -> dict:
    """Write the model into a tenant graph. Same schema as the demo tenants, so every agent tool works."""
    g = db.graph(tenant)
    if reset:
        try:
            g.delete()
        except Exception:
            pass
        g = db.graph(tenant)
    schema.create_indexes(g)
    model: rm.SystemModel = data["model"]
    labels = {"service": "Service", "datastore": "Datastore", "topic": "Topic"}
    for kind, label in labels.items():
        rows = [{"name": n, "tier": c["tier"], "kind": kind, "language": c.get("language"),
                 "paths": c["paths"]} for n, c in model.components.items() if c["kind"] == kind]
        if rows:
            g.query(f"UNWIND $rows AS r MERGE (c:Component:{label} {{name: r.name}}) "
                    "SET c.tier = r.tier, c.kind = r.kind, c.language = r.language, c.paths = r.paths",
                    {"rows": rows})
    for rel in ("CALLS", "READS_WRITES", "PUBLISHES"):
        rows = [{"src": s, "dst": d} for s, d, r in model.deps if r == rel]
        if rows:
            g.query(f"UNWIND $rows AS r MATCH (a:Component {{name: r.src}}), (b:Component {{name: r.dst}}) "
                    f"MERGE (a)-[:{rel}]->(b)", {"rows": rows})
    if model.flows:
        g.query("UNWIND $rows AS r MERGE (f:UserFlow {name: r.flow}) "
                "WITH f, r MATCH (c:Component {name: r.comp}) MERGE (c)-[:SERVES]->(f)",
                {"rows": [{"flow": f, "comp": c} for f, comps in model.flows.items() for c in comps]})
    g.query("UNWIND $rows AS r MERGE (p:Person {handle: r.handle}) SET p.name = r.name, p.role = 'engineer'",
            {"rows": [{"handle": h, "name": n} for h, n in data["people"].items()]})
    g.query("UNWIND $rows AS r MERGE (t:Team {name: r.name}) "
            "WITH t, r UNWIND r.owns AS comp MATCH (c:Component {name: comp}) MERGE (t)-[:OWNS]->(c)",
            {"rows": [t for t in data["teams"] if t["owns"]]})
    g.query("UNWIND $rows AS r MATCH (t:Team {name: r.team}), (p:Person {handle: r.handle}) MERGE (p)-[:MEMBER_OF]->(t)",
            {"rows": [{"team": t["name"], "handle": m} for t in data["teams"] for m in t["members"]]})
    g.query("UNWIND $rows AS r MERGE (pr:PullRequest {number: r.number}) "
            "SET pr.title = r.title, pr.merged_at = r.merged_at, pr.url = r.url "
            "WITH pr, r MATCH (p:Person {handle: r.author}) MERGE (p)-[:AUTHORED]->(pr) "
            "WITH pr, r UNWIND r.components AS comp MATCH (c:Component {name: comp}) MERGE (pr)-[:MODIFIES]->(c)",
            {"rows": data["changes"]})
    if data["decisions"]:
        g.query("UNWIND $rows AS r MERGE (d:Decision {id: r.id}) "
                "SET d.title = r.title, d.status = r.status, d.date = r.date, d.rationale = r.rationale "
                "MERGE (doc:Document {id: r.id + '-doc'}) "
                "SET doc.kind = 'adr', doc.title = r.id + ': ' + r.title, doc.body = r.rationale, doc.url = r.url "
                "MERGE (d)-[:RECORDED_IN]->(doc) "
                "WITH d, doc, r UNWIND r.affects AS comp MATCH (c:Component {name: comp}) "
                "MERGE (d)-[:AFFECTS]->(c) MERGE (doc)-[:ABOUT]->(c)",
                {"rows": [{**d, "url": f"{data['source']['url']}/blob/{data['source']['ref']}/{d['path']}"}
                          for d in data["decisions"]]})
    memory.derive(g)
    memory.embed_missing(g)
    src = data["source"]
    g.query("MERGE (m:Meta {key: 'dataset'}) SET m.generated_at = timestamp() / 1000, m.source = $repo, "
            "m.url = $url, m.topology_from = $topo, m.entry = $entry, m.real_names = $real",
            {"repo": src["repo"], "url": src["url"], "topo": src["topology_from"], "entry": model.entry,
             "real": src["real_names"]})
    counts = db.rows(g.query("MATCH (n) RETURN labels(n)[0] AS label, count(*) AS n ORDER BY label"))
    return {r["label"]: r["n"] for r in counts}


def connect(repo_ref: str, max_prs: int | None = None, real_names: bool = False,
            progress: Callable[[str], None] = lambda msg: None) -> dict:
    owner, name = parse_repo(repo_ref)
    tenant = tenant_for(owner, name)
    data = build(repo_ref, max_prs, real_names, progress=progress)
    progress("Writing the graph")
    counts = load(tenant, data)
    model = data["model"]
    return {"tenant": tenant, "repo": data["source"]["repo"], "topology_from": model.topology_from,
            "entry": model.entry, "components": len(model.components), "dependencies": len(model.deps),
            "teams": len(data["teams"]), "people": len(data["people"]), "pull_requests": len(data["changes"]),
            "decisions": len(data["decisions"]), "api_calls": data["source"]["api_calls"], "nodes": counts}


def main() -> None:
    ap = argparse.ArgumentParser(description="Connect a public GitHub repository as a tenant graph")
    ap.add_argument("repo", help="owner/repo or https://github.com/owner/repo")
    ap.add_argument("--max-prs", type=int)
    ap.add_argument("--real-names", action="store_true", help="show GitHub logins instead of pseudonyms")
    args = ap.parse_args()
    try:
        out = connect(args.repo, args.max_prs, args.real_names, progress=lambda m: print("..", m, flush=True))
    except ConnectorError as e:
        raise SystemExit(f"error: {e}")
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()

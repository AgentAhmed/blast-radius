"""Turn repository files into a system model: components, dependencies, owners, changes, decisions.

Pure functions (no network), so every rule here is unit-tested. Sources, in order of preference:
  blastradius.yaml     explicit catalog: components, kinds, paths, dependencies, flows, teams
  docker-compose.yml   services, `depends_on`, and service hostnames referenced in environment variables
  CODEOWNERS           which team/people own which paths (last matching rule wins, as on GitHub)
  ADR markdown files   design decisions, linked to the components they mention
"""
import hashlib
import posixpath
import re
from dataclasses import dataclass, field
from datetime import datetime

import yaml

# Observability and tooling containers are not part of the system's request path.
NOT_PART_OF_SYSTEM = re.compile(
    r"otel|collector|jaeger|grafana|prometheus|opensearch|elasticsearch-exporter|loki|tempo|zipkin|kibana|"
    r"alloy|promtail|cadvisor|node-exporter|portainer|adminer|pgadmin|mailhog|mailpit|opamp", re.I)
DATASTORE = re.compile(r"postgres|mysql|mariadb|mongo|redis|valkey|memcached|cassandra|elasticsearch|clickhouse|"
                       r"cockroach|minio|dynamo|couchbase|neo4j|falkordb|sqlserver|mssql|oracle", re.I)
BROKER = re.compile(r"kafka|rabbitmq|nats|pulsar|redpanda|activemq|zookeeper|eventstore", re.I)
LOAD_GENERATOR = re.compile(r"load|locust|k6|jmeter|gatling|test", re.I)
ADR_PATH = re.compile(r"(^|/)(adr|adrs|decisions|architecture-decisions|architecture/decisions)/[^/]+\.md$", re.I)
CODEOWNERS_PATHS = ["CODEOWNERS", ".github/CODEOWNERS", "docs/CODEOWNERS"]
COMPOSE_PATHS = ["docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml"]
COMPOSE_FILE = re.compile(r"^(docker-)?compose([.-][\w.-]+)?\.ya?ml$", re.I)
CATALOG_PATH = "blastradius.yaml"
PSEUDONYM_SALT = "blast-radius"


@dataclass
class SystemModel:
    components: dict[str, dict] = field(default_factory=dict)   # name -> {kind, tier, paths, language}
    deps: set[tuple[str, str, str]] = field(default_factory=set)  # (src, dst, CALLS|READS_WRITES|PUBLISHES)
    flows: dict[str, list[str]] = field(default_factory=dict)
    entry: str | None = None
    topology_from: str = "none"


# --- People ------------------------------------------------------------------------------------

def pseudonym(login: str) -> str:
    """Stable, unique, non-reversible id for a GitHub user: same login -> same id, every import."""
    return "dev-" + hashlib.sha256(f"{PSEUDONYM_SALT}:{login.lower()}".encode()).hexdigest()[:6]


def person_id(login: str, real_names: bool) -> str:
    login = login.lstrip("@")
    return login.lower() if real_names else pseudonym(login)


# --- Topology ----------------------------------------------------------------------------------

def _kind(name: str, image: str = "") -> str:
    text = f"{name} {image}"
    if BROKER.search(text):
        return "topic"
    if DATASTORE.search(text):
        return "datastore"
    return "service"


def _norm_path(path: str) -> str:
    path = posixpath.normpath(path.replace("\\", "/")).lstrip("/")
    return "" if path in (".", "") else path


def _build_path(build) -> str | None:
    """Source directory of a compose service from its `build` section."""
    if isinstance(build, str):
        return _norm_path(build) or None
    if isinstance(build, dict):
        context = _norm_path(build.get("context", "."))
        dockerfile = build.get("dockerfile")
        if dockerfile and "/" in dockerfile.replace("\\", "/"):
            return _norm_path(posixpath.join(context, posixpath.dirname(dockerfile.replace("\\", "/")))) or None
        return context or None
    return None


def _env_values(env) -> list[str]:
    if isinstance(env, dict):
        return [str(v) for v in env.values() if v is not None]
    if isinstance(env, list):
        return [str(e).split("=", 1)[1] for e in env if "=" in str(e)]
    return []


def compose_files(files: set[str]) -> list[str]:
    """Root-level compose files, base file first, then overrides; test compose files are skipped."""
    found = [f for f in files if COMPOSE_FILE.match(f) and not re.search(r"test", f, re.I)]
    return sorted(found, key=lambda f: (f not in COMPOSE_PATHS, f))


def merge_compose(texts: list[str]) -> dict:
    """Merge compose files the way `docker compose -f a -f b` does, for the fields we use."""
    services: dict[str, dict] = {}
    for text in texts:
        for name, spec in ((yaml.safe_load(text) or {}).get("services") or {}).items():
            spec = spec or {}
            merged = services.setdefault(name, {"depends_on": [], "environment": [], "ports": []})
            for key in ("build", "image"):
                if spec.get(key):
                    merged[key] = spec[key]
            depends = spec.get("depends_on") or []
            merged["depends_on"] += list(depends.keys() if isinstance(depends, dict) else depends)
            merged["environment"] += [f"_={v}" for v in _env_values(spec.get("environment"))]
            merged["ports"] += spec.get("ports") or []
    return {"services": services}


def from_compose(texts: list[str] | str) -> SystemModel:
    data = merge_compose([texts] if isinstance(texts, str) else texts)
    services = {n: s for n, s in data["services"].items() if not NOT_PART_OF_SYSTEM.search(n)}
    model = SystemModel(topology_from="docker-compose")
    for name, spec in services.items():
        path = _build_path(spec.get("build"))
        model.components[name] = {"kind": _kind(name, str(spec.get("image", ""))), "tier": None,
                                  "paths": [path] if path else [], "language": None}
    names = sorted(model.components, key=len, reverse=True)
    for name, spec in services.items():
        targets = set()
        depends = spec.get("depends_on") or []
        targets.update(depends.keys() if isinstance(depends, dict) else depends)
        for value in _env_values(spec.get("environment")):
            for other in names:
                if other != name and re.search(rf"(?<![\w.-]){re.escape(other)}(?=[:/]|$)", value):
                    targets.add(other)
        for t in targets:
            if t in model.components and t != name:
                model.deps.add((name, t, _relation(model.components[t]["kind"])))
    _assign_tiers(model, has_ports={n for n, s in services.items() if s.get("ports")})
    return model


def from_catalog(text: str) -> tuple[SystemModel, list[dict]]:
    """blastradius.yaml -> (model, teams). See docs/CONNECTORS.md for the format."""
    data = yaml.safe_load(text) or {}
    model = SystemModel(topology_from="blastradius.yaml", entry=data.get("entrypoint"))
    for c in data.get("components") or []:
        model.components[c["name"]] = {
            "kind": c.get("kind", "service"), "tier": c.get("tier"), "language": c.get("language"),
            "paths": [_norm_path(p) for p in c.get("paths") or []],
        }
    for c in data.get("components") or []:
        for t in c.get("depends_on") or []:
            if t in model.components:
                model.deps.add((c["name"], t, _relation(model.components[t]["kind"])))
        for flow in c.get("flows") or []:
            model.flows.setdefault(flow, []).append(c["name"])
    _assign_tiers(model, has_ports=set())
    teams = [{"name": t["name"], "owns": t.get("owns") or [], "members": t.get("members") or []}
             for t in data.get("teams") or []]
    return model, teams


def _relation(target_kind: str) -> str:
    return {"datastore": "READS_WRITES", "topic": "PUBLISHES"}.get(target_kind, "CALLS")


def _assign_tiers(model: SystemModel, has_ports: set[str]) -> None:
    incoming: dict[str, int] = {n: 0 for n in model.components}
    for src, dst, _ in model.deps:
        if not LOAD_GENERATOR.search(src):          # synthetic traffic does not make a service internal
            incoming[dst] += 1
    if not model.entry:
        roots = [n for n, c in model.components.items() if c["kind"] == "service" and incoming[n] == 0
                 and not LOAD_GENERATOR.search(n)]
        exposed = [n for n in roots if n in has_ports]
        candidates = exposed or roots
        # The entry point is the root that reaches the most of the system.
        model.entry = max(candidates, key=lambda n: (_reach(model, n), n), default=None)
    for name, c in model.components.items():
        if c.get("tier"):
            continue
        if name == model.entry:
            c["tier"] = "edge"
        elif c["kind"] != "service" or incoming[name] >= 2:
            c["tier"] = "core"
        else:
            c["tier"] = "support"


def _reach(model: SystemModel, start: str) -> int:
    seen, stack = set(), [start]
    while stack:
        n = stack.pop()
        for src, dst, _ in model.deps:
            if src == n and dst not in seen:
                seen.add(dst)
                stack.append(dst)
    return len(seen)


def add_path_guesses(model: SystemModel, tree_dirs: set[str]) -> None:
    """Components without a known source path: use a directory named exactly like the component."""
    for name, c in model.components.items():
        if c["paths"] or c["kind"] != "service":
            continue
        for prefix in ("src/", "services/", "apps/", "cmd/", ""):
            if f"{prefix}{name}" in tree_dirs:
                c["paths"] = [f"{prefix}{name}"]
                break


def components_for_file(model: SystemModel, path: str) -> list[str]:
    """Components whose source directory contains the file (longest match wins)."""
    best, best_len = [], 0
    for name, c in model.components.items():
        for p in c["paths"]:
            if p and (path == p or path.startswith(p + "/")):
                if len(p) > best_len:
                    best, best_len = [name], len(p)
                elif len(p) == best_len:
                    best.append(name)
    return best


# --- Ownership -------------------------------------------------------------------------------

def parse_codeowners(text: str) -> list[tuple[str, list[str]]]:
    rules = []
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        pattern, *owners = line.split()
        if owners:
            rules.append((pattern, owners))
    return rules


def _pattern_matches(pattern: str, paths: list[str]) -> bool:
    if pattern in ("*", "/*", "**", "/**"):
        return True
    p = pattern.strip("/").rstrip("*").rstrip("/")
    if not p:
        return True
    if "*" in p:                                    # e.g. src/*/Dockerfile -> compare the fixed prefix only
        p = p.split("*", 1)[0].rstrip("/")
    return any(path == p or path.startswith(p + "/") or p.startswith(path + "/") for path in paths if path)


def owners_by_component(model: SystemModel, rules: list[tuple[str, list[str]]]) -> dict[str, list[str]]:
    """Last matching CODEOWNERS rule wins, as on GitHub."""
    owners = {}
    for name, c in model.components.items():
        for pattern, rule_owners in rules:
            if _pattern_matches(pattern, c["paths"]) or (pattern in ("*", "/*", "**") and not c["paths"]):
                owners[name] = rule_owners
    return owners


def teams_from_owners(owners: dict[str, list[str]], real_names: bool) -> list[dict]:
    """@org/team owners become teams; individual owners become '<component>-owners' teams."""
    teams: dict[str, dict] = {}
    for component, entries in owners.items():
        org_teams = [e for e in entries if e.startswith("@") and "/" in e]
        # "@user" or "user@example.com" (email owners: the part before @)
        people = [person_id(e if e.startswith("@") else e.split("@")[0], real_names)
                  for e in entries if e not in org_teams]
        for t in org_teams:
            teams.setdefault(t.split("/", 1)[1], {"owns": set(), "members": set()})["owns"].add(component)
        if people and not org_teams:
            team = teams.setdefault(f"{component}-owners", {"owns": set(), "members": set()})
            team["owns"].add(component)
            team["members"].update(people)
    return [{"name": n, "owns": sorted(t["owns"]), "members": sorted(t["members"])} for n, t in sorted(teams.items())]


# --- Decisions -------------------------------------------------------------------------------

def parse_adr(path: str, text: str, components: list[str], number: int) -> dict:
    lines = text.splitlines()
    title = next((l.lstrip("# ").strip() for l in lines if l.startswith("#")), posixpath.basename(path))
    status = _field(text, "status") or "unknown"
    date = _field(text, "date")
    body = re.sub(r"\s+", " ", re.sub(r"^#.*$", "", text, flags=re.M)).strip()
    mentioned = sorted(c for c in components if re.search(rf"(?<![\w-]){re.escape(c)}(?![\w-])", text, re.I))
    return {"id": f"ADR-{number:03d}", "title": title[:200], "status": status.lower()[:40],
            "date": _epoch(date), "rationale": body[:800], "affects": mentioned, "path": path}


def _field(text: str, name: str) -> str | None:
    m = re.search(rf"^\W*{name}\W*[:\-]\s*(.+)$", text, re.I | re.M)
    return m.group(1).strip() if m else None


def _epoch(value: str | None) -> int | None:
    if not value:
        return None
    m = re.search(r"\d{4}-\d{2}-\d{2}", value)
    return int(datetime.fromisoformat(m.group(0)).timestamp()) if m else None


def iso_to_epoch(value: str) -> int:
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())

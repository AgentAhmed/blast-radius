# Connectors: run Blast Radius on real data

Connectors load real data into a **new tenant graph**, using the same schema as the demo tenants. Every agent,
tool, query and UI view works on it unchanged.

## 1. GitHub repository
```bash
python -m blastradius.connectors.github owner/repo [--max-prs 60] [--real-names]
```
Or use **Connect a GitHub repo** in the console. This creates tenant `gh-<owner>-<repo>`.

| What | From | Notes |
|---|---|---|
| Components and dependencies | `blastradius.yaml` if present, otherwise all root compose files (`docker-compose*.yml`, `compose*.yaml`, test files skipped) merged like `docker compose -f a -f b` | Dependencies come from `depends_on` **and** from other services' hostnames in environment variables (`cart:7070`, `http://payment:8080`). Observability containers (collector, Jaeger, Grafana, Prometheus, ...) are left out. |
| Component kind | image or name | postgres/redis/valkey/mongo/... → datastore; kafka/rabbitmq/nats/... → topic; else service |
| Source paths | compose `build` context or dockerfile directory; else a directory named like the service (`src/<name>`, `services/<name>`, ...) | Used to map changed files to components |
| Entry point and tiers | the root service that reaches the most of the system (load generators ignored) | Used by shortest-path-to-user and the graph layout |
| Owners | `CODEOWNERS` (last matching rule wins, as on GitHub) | `@org/team` → team; individual owners → a `<component>-owners` team |
| Changes and experts | merged pull requests and their changed files | Bots are skipped. Each PR is linked to every component whose files it changed. |
| Design decisions | Markdown files under `adr/`, `adrs/`, `decisions/`, `architecture/decisions/` (up to 12) | Title, status, date. Linked to every component the text names. |

**People:** each GitHub login becomes a stable pseudonym, `dev-` plus 6 hex characters from a salted SHA-256.
The same person gets the same id in every PR and every import, so experts and ownership stay correct, but the
login is not stored. `--real-names` keeps logins instead.

**Limits:**
- Only **public** repositories are supported. That keeps us to public data (hackathon rule 6), and no private
  tokens are stored.
- **API budget:** about 3 calls plus one per PR. GitHub allows 60 calls/hour without a token and 5,000 with
  `GITHUB_TOKEN`. `--max-prs` defaults to 40 without a token and 150 with one. Files are read from
  `raw.githubusercontent.com`, which doesn't count against the limit.
- Without compose files or a catalog, there are no dependencies, so root-cause tracing has nothing to follow. Add a
  `blastradius.yaml`.
- A repo has no on-call schedule, so the investigator pages the person who changed the root-cause component most.

### `blastradius.yaml` catalog (optional, at the repo root)
```yaml
entrypoint: web
components:
  - name: web
    paths: [apps/web]
    depends_on: [api]
    flows: [checkout]          # user flows this component serves (sets incident severity)
  - name: api
    paths: [services/api]
    depends_on: [db, orders]
  - name: db
    kind: datastore
  - name: orders
    kind: topic
teams:
  - name: Core
    owns: [api, db]
    members: [alice, bob]      # GitHub logins (pseudonymized unless --real-names)
```

## 2. Alerts webhook
`POST /api/alerts?tenant=<tenant>` accepts:

**Prometheus Alertmanager** (configure a `webhook_configs` receiver with this URL; Grafana alerting can post the
same format):
```json
{"version": "4", "status": "firing", "alerts": [
  {"status": "firing", "labels": {"alertname": "HighErrorRate", "service": "payment", "severity": "critical"},
   "annotations": {"summary": "5xx > 5%"}, "startsAt": "2026-10-15T11:00:00Z"}]}
```
**Generic JSON**: `{"component": "payment", "signal": "error_rate", "severity": "critical", "value": 0.4}`, a list of
these, or `{"alerts": [...]}`.

**Matching labels to components:**
- The first label present among `component`, `service`, `service_name`, `service.name`, `app`,
  `app.kubernetes.io/name`, `deployment`, `container`, `job`, `pod` is used.
- Matching tries the exact name first, then the name without `-service`/`-svc`/`-server`/`-api`/`-app`, then pod or
  replica-set names (`checkout-5c9d7f-q2w8r` → `checkout`).
- Unmatched values are returned in the response, never guessed.

**Lifecycle:**
- Firing alerts are triaged into an incident and investigated, the same as simulated ones.
- `status: resolved` alerts close the matching firing alerts.

```bash
curl -X POST "http://localhost:8007/api/alerts?tenant=gh-open-telemetry-opentelemetry-demo" \
  -H "Content-Type: application/json" \
  -d '{"alerts":[{"status":"firing","labels":{"alertname":"HighErrorRate","service":"payment","severity":"critical"}}]}'
```

## Verified on a real repository
Connecting [open-telemetry/opentelemetry-demo](https://github.com/open-telemetry/opentelemetry-demo) produced
26 components and 43 dependencies from its 4 compose files, plus real merged PRs and the `CODEOWNERS` team. The
entry point it detected was `frontend-proxy`. It found 16 of the 27 dependencies in our hand-written demo model.
The others differ because the current repo has moved on: it now has `astronomy-db`, an AI agent and a chatbot
service, and calls `flagd` from every service. An Alertmanager payload using `service`, `pod` and `app` labels was
mapped to the right components, and the investigator traced it to `payment`.

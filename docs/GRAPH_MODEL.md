# Graph data model

One graph per tenant in a single FalkorDB instance: `org_astroshop`, `org_nebula`, and so on. Every request names
its tenant, and a query can only see that tenant's graph.

## 1. System
| Node | Properties | Notes |
|---|---|---|
| `Component:Service` | name, language, tier, criticality | 18 services of the OpenTelemetry Demo |
| `Component:Datastore` | name, kind, tier, criticality | valkey-cart, postgresql |
| `Component:Topic` | name, kind, tier, criticality | Kafka topic `orders` |
| `UserFlow` | name | browse, cart, checkout, order-processing |

| Edge | From → To | Meaning |
|---|---|---|
| `CALLS {protocol}` | Service → Service | Synchronous call (grpc/http) |
| `READS_WRITES` | Service → Datastore | Storage dependency |
| `PUBLISHES` / `CONSUMES` | Service → Topic | Asynchronous dependency |
| `DEPENDS_ON {via}` | Component → Component | **Derived** from the four above, for algorithms |
| `SERVES` | Component → UserFlow | Business impact |

`Component` is a shared label, so one traversal crosses services, databases and topics. The specific label
(`Service`, `Datastore`, `Topic`) is still there for filtering.

## 2. Organisation and change
| Node | Properties |
|---|---|
| `Team` | name |
| `Person` | handle, name, role (lead/engineer). All people are fictional. |
| `PullRequest` | number, title, merged_at |
| `Release` | id, version, deployed_at |

| Edge | Meaning |
|---|---|
| `(Team)-[:OWNS]->(Component)` | Ownership |
| `(Person)-[:MEMBER_OF]->(Team)` | Team membership |
| `(Person)-[:ON_CALL_FOR {from, to}]->(Team)` | On-call shift. **Time lives on the edge**, so "now" is a filter. |
| `(Person)-[:AUTHORED]->(PullRequest)-[:MODIFIES]->(Component)` | Who changed what. Gives expertise and suspects. |
| `(Release)-[:DEPLOYS]->(Component)`, `(Release)-[:INCLUDES]->(PullRequest)` | What shipped, and when |

## 3. Knowledge
| Node | Properties |
|---|---|
| `Decision` | id (ADR-00x), title, date, status, rationale |
| `Document` | id, kind (adr / postmortem), title, body, **embedding** (vector index) |
| `Runbook` | id, title, steps |
| `Lesson` | id, text, incident, at. Written by the scribe. |

| Edge | Meaning |
|---|---|
| `(Person)-[:MADE]->(Decision)-[:AFFECTS]->(Component)` | Who decided what, and what it touches |
| `(Decision)-[:RECORDED_IN]->(Document)` | Source of truth |
| `(Document)-[:ABOUT]->(Component)`, `(Document)-[:POSTMORTEM_OF]->(Incident)` | What a document is about |
| `(Runbook)-[:FOR]->(Component)` | Procedural knowledge |
| `(Lesson)-[:APPLIES_TO]->(Component)`, `(Lesson)-[:LEARNED_FROM]->(Incident)` | Learned procedural memory |

## 4. Incidents (episodic memory)
| Node | Properties |
|---|---|
| `Alert` | id, signal (error_rate / latency_p99 / consumer_lag), severity, value, fired_at, status |
| `Incident` | id, title, severity, status (investigating → identified → resolving → resolved), opened_at, resolved_at, summary, **embedding** |
| `Finding` | id, root_cause, confidence, summary, evidence[], suspect_change, recommended_action |
| `Action` | id, kind (page / github_issue / rollback / flag-off / ...), status, url, detail |
| `Scenario` | flag, root, started_at. **Ground truth of a simulated fault; never read by agent tools.** |

| Edge | Meaning |
|---|---|
| `(Alert)-[:FIRED_ON]->(Component)` | Symptom |
| `(Incident)-[:TRIGGERED_BY]->(Alert)`, `-[:IMPACTED]->(Component)` | What the incident is made of |
| `(Incident)-[:HAS_FINDING]->(Finding)-[:BLAMES]->(Component)` | The agent's conclusion |
| `(Finding)-[:SUPPORTED_BY]->(Component / PullRequest / Incident / Person / ...)` | **Evidence: what the finding cites** |
| `(Incident)-[:PAGED {reason, at}]->(Person)`, `(Incident)-[:HAS_ACTION]->(Action)` | What the agent did |
| `(Incident)-[:ROOT_CAUSE]->(Component)`, `-[:RESOLVED_BY]->(Action)` | Confirmed outcome |
| `(Incident)-[:SIMILAR_TO {score}]->(Incident)` | Memory links |

## 5. Agents (coordination and audit)
| Node | Properties |
|---|---|
| `AgentRun` | id, agent, goal, status (running / paused / done / failed), resume_at, provider, system_prompt, user_prompt, session, result |
| `Step` | id, run, n, kind (llm / tool / note / pause), tool, args, output, message, provider, model, tokens, latency_ms, at |
| `Task` | id, type (investigate / record), status (open / claimed / done / failed), incident, payload, claimed_by |

| Edge | Meaning |
|---|---|
| `(AgentRun)-[:HAS_STEP]->(Step)-[:NEXT]->(Step)` | Ordered trail = **checkpoint** |
| `(Step)-[:TOUCHED]->(any entity)` | What each step read or wrote |
| `(Step)-[:PRODUCED]->(Finding)` | Which step concluded |
| `(AgentRun)-[:INVESTIGATES]->(Incident)`, `(AgentRun)-[:WORKED_ON]->(Task)` | Context |
| `(Task)-[:FOR]->(Incident)` | Handoff |

## Indexes
- Range indexes on lookup keys, such as `Component.name`, `Person.handle`, `Incident.id`, `Task.status`,
  `AgentRun.status` and `PullRequest.merged_at`.
- **Vector indexes:** `Incident.embedding`, `Document.embedding` (384 dimensions, cosine).
- **Full-text index:** `Document(title, body)`.

## Modelling decisions to defend
1. **Incidents, findings and lessons are nodes, not properties.** They connect to many things, and those connections
   are what make recall and explanations possible.
2. **Time on edges** (`ON_CALL_FOR {from,to}`) and on nodes (`merged_at`, `deployed_at`), so questions about "now" and
   "recently" are plain filters.
3. **`DEPENDS_ON` is derived, not authored.** The source edges keep their meaning (call vs. publish), and the derived
   edge serves the algorithms.
4. **Agent state is graph data.** Checkpoints, handoffs and provenance live next to the facts they refer to, so
   "which steps touched payment?" is a single query.
5. **`Scenario` is separate from `Alert`.** Agents see symptoms, never the answer.

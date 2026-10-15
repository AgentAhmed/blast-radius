# Agents, runtime and LLM gateway

## The agents
| Agent | Uses LLM? | Input | Output | Tools |
|---|---|---|---|---|
| **Triage** | No | Firing alerts not yet in an incident | `Incident` (severity from user flows at risk), `Task{investigate}` | — |
| **Investigator** | Yes (≤ 7 calls) | Incident with alerts | `Finding` with evidence; pages the on-call person; opens a GitHub issue | root_cause_candidates, recent_changes, similar_incidents, past_fixes, blast_radius, path_to_user, component_overview, who_to_page, criticality → `submit_findings` |
| **Scribe** | Yes (1 call) | Resolved incident and its fix | Postmortem `Document`, `Lesson`, embeddings, `SIMILAR_TO` links | → `write_postmortem` |
| **Company brain** | Yes (≤ 6 calls) | A question, plus earlier turns in the session | Answer with sources | 15 read tools → `final_answer` |

Severity rule (triage): any alerting component serving the `checkout` flow → SEV1. Three or more components, or
order processing → SEV2. Otherwise SEV3.

## The agent loop ([runtime.py](../blastradius/agents/runtime.py))
```
execute(run):
    messages = system + user + rebuild from Step nodes        # same code for new, paused and crashed runs
    loop:
        run any tool calls the model asked for that have no result yet   -> Step(kind=tool) + TOUCHED
            a terminal tool (submit_findings / final_answer / write_postmortem) ends the run
        if over budget: tell the model to submit now
        ask LLM gateway                                         -> Step(kind=llm)
            all providers busy -> Step(kind=pause), run.status = paused, resume_at
```
Key properties:
- **Checkpoint = the step trail.** Nothing is kept only in memory. `_rebuild` turns `Step` nodes back into chat messages.
- **No repeated work on resume.** Completed tool calls are in the trail, and only missing ones are run.
- **Validation.** A terminal result can be rejected (for example, a root cause that isn't a known component). The
  error goes back to the model, which tries again.
- **Order.** If the model calls the final tool in the same turn as other tools, the final tool runs last.
- **Readable times.** Epoch timestamps in tool results become "2h ago" before the model sees them.

## Coordination ([store.py](../blastradius/agents/store.py), [worker.py](../blastradius/agents/worker.py))
- Agents never call each other. They create `Task` nodes, and a worker claims a task with **one Cypher query**.
  Writes to a FalkorDB graph run one at a time, so two workers can never claim the same task.
- The worker loop: triage new alerts → claim and run tasks → resume paused runs whose `resume_at` has passed.
- The API process runs one worker thread. More can run with `python -m blastradius.agents.worker`.

## Memory types
| Type | In the graph | Written by | Read by |
|---|---|---|---|
| **Episodic** (what happened) | `Incident`, `Alert`, `Finding`, `Action`, `SIMILAR_TO` | triage, investigator, scribe | `similar_incidents`, `past_fixes`, `incident_details` |
| **Semantic** (how things are) | topology, ownership, PRs, decisions, documents with embeddings | ingest | most tools |
| **Procedural** (how to fix) | `Runbook`, `Lesson`, `Action` kinds | ingest, scribe | `past_fixes` |
| **Conversation** | `AgentRun {agent:'brain', session}` | brain | brain (last 3 turns of the session) |
| **Per tenant** | a separate graph per org | — | everything |

## LLM gateway ([llm/gateway.py](../blastradius/llm/gateway.py))
- Slots, in order: `groq, gemini, openrouter, custom, ollama` (`LLM_PROVIDER_ORDER`).
- Each slot has several models, tried in order: `GROQ_MODELS=...`.
- A slot with no key is skipped. Ollama is skipped when it isn't running.
- Each provider has a requests-per-minute budget (sliding 60 s window). If waiting would take more than 5 s, the
  gateway moves to the next provider.
- Error handling: 429 → that model cools down for `retry-after`. 5xx, timeout or connection error → the provider
  cools down for 30 s. 401/403 → provider disabled. 404 → model disabled. 400 → next model.
- When nothing can answer, it raises `AllProvidersExhausted(resume_at)`, which the runtime turns into a pause.
- Each LLM step records which provider and model answered, and any fallbacks on the way. The UI shows it.

Typical cost: an investigation takes 4–7 LLM calls and the scribe 1. Graph tools do the reasoning-heavy work for
free.

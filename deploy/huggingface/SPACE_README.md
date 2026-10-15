---
title: Blast Radius
emoji: 💥
colorFrom: red
colorTo: indigo
sdk: docker
app_port: 7860
pinned: false
license: mit
short_description: AI on-call agents on a FalkorDB knowledge graph
---

# Blast Radius

AI on-call agents that triage, investigate and remember incidents, using a FalkorDB knowledge graph of services,
owners, changes and past incidents.

**Try it:** pick a fault under *Inject a fault* and watch the agents trace it to a root cause, cite the change
and page the right person. Ask the *Company brain* questions, or use *Connect a GitHub repo* to load any public
repository with a `docker-compose` file.

This Space runs FalkorDB and the app in one container on the free tier. Its data resets when the Space restarts,
and the LLM is a free-tier key, so an investigation can pause on rate limits and resume by itself.

Source, docs and how to run it locally: https://github.com/AgentAhmed/blast-radius

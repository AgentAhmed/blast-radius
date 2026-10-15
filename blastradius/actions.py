"""Actions agents take in the outside world. Every action is an Action node, created once (idempotent).

- page: notify the on-call person (recorded in the graph; the UI shows it as a page)
- github_issue: open an issue on GITHUB_REPO if GITHUB_TOKEN is set, otherwise recorded as a dry run
- notify: post the incident to Slack and/or Discord through incoming webhooks (SLACK_WEBHOOK_URL,
  DISCORD_WEBHOOK_URL); skipped when neither is set
"""
import json
import os
import time
import urllib.request

from blastradius import db

GET_ACTION = "MATCH (a:Action {id: $id}) RETURN a.status AS status, a.url AS url"

RECORD_ACTION = """
MATCH (i:Incident {id: $incident})
MERGE (a:Action {id: $id})
SET a.kind = $kind, a.status = $status, a.url = $url, a.detail = $detail, a.at = $now
MERGE (i)-[:HAS_ACTION]->(a)
"""

PAGE = """
MATCH (i:Incident {id: $incident}), (p:Person {handle: $handle})
MERGE (i)-[pg:PAGED]->(p)
SET pg.at = $now, pg.reason = $reason
"""


def page(tenant, incident: str, handle: str, reason: str) -> dict:
    g = db.graph(tenant)
    action_id = f"{incident}-page-{handle}"
    if db.rows(g.ro_query(GET_ACTION, {"id": action_id})):
        return {"id": action_id, "status": "already_done"}
    now = int(time.time())
    g.query(PAGE, {"incident": incident, "handle": handle, "now": now, "reason": reason})
    g.query(RECORD_ACTION, {"incident": incident, "id": action_id, "kind": "page", "status": "sent",
                            "url": None, "detail": f"Paged {handle}: {reason}", "now": now})
    return {"id": action_id, "status": "sent"}


def github_issue(tenant, incident: str, title: str, body: str) -> dict:
    g = db.graph(tenant)
    action_id = f"{incident}-issue"
    existing = db.rows(g.ro_query(GET_ACTION, {"id": action_id}))
    if existing and existing[0]["status"] == "created":
        return {"id": action_id, "status": "already_done", "url": existing[0]["url"]}

    token, repo = os.getenv("GITHUB_TOKEN"), os.getenv("GITHUB_REPO")
    if repo:   # accept "owner/repo" or a full https://github.com/owner/repo URL
        repo = repo.strip().removesuffix(".git").removeprefix("https://github.com/").strip("/")
    status, url = "dry_run", None
    if token and repo:
        req = urllib.request.Request(
            f"https://api.github.com/repos/{repo}/issues", method="POST",
            data=json.dumps({"title": title, "body": body, "labels": ["incident"]}).encode(),
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                     "User-Agent": "blast-radius"},
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                url = json.load(resp)["html_url"]
                status = "created"
        except OSError as e:
            status = f"failed: {e}"
    g.query(RECORD_ACTION, {"incident": incident, "id": action_id, "kind": "github_issue", "status": status,
                            "url": url, "detail": title, "now": int(time.time())})
    return {"id": action_id, "status": status, "url": url}


CHANNELS = {"slack": "SLACK_WEBHOOK_URL", "discord": "DISCORD_WEBHOOK_URL"}


def _chat_body(channel: str, headline: str, lines: list[str], link: str | None) -> dict:
    """Slack and Discord both render markdown, with different bold and link syntax."""
    if channel == "slack":
        text = f"*{headline}*\n" + "\n".join(lines) + (f"\n<{link}|Tracking issue>" if link else "")
        return {"text": text}
    text = f"**{headline}**\n" + "\n".join(lines) + (f"\nTracking issue: <{link}>" if link else "")
    return {"username": "Blast Radius", "content": text[:1990]}


def notify(tenant, incident: str, event: str, headline: str, lines: list[str], link: str | None = None) -> list[dict]:
    """Post one message per configured channel for this incident event ("identified", "resolved"). Idempotent."""
    g = db.graph(tenant)
    results = []
    for channel, env in CHANNELS.items():
        url = os.getenv(env)
        if not url:
            continue
        action_id = f"{incident}-{channel}-{event}"
        existing = db.rows(g.ro_query(GET_ACTION, {"id": action_id}))
        if existing and existing[0]["status"] == "sent":
            results.append({"id": action_id, "status": "already_done"})
            continue
        req = urllib.request.Request(
            url, method="POST", data=json.dumps(_chat_body(channel, headline, lines, link)).encode(),
            headers={"Content-Type": "application/json", "User-Agent": "blast-radius"},
        )
        try:
            with urllib.request.urlopen(req, timeout=10):
                status = "sent"
        except OSError as e:
            # Never store the webhook URL itself (it is a secret); only the error type.
            status = f"failed: {type(e).__name__} {getattr(e, 'code', '')}".strip()
        g.query(RECORD_ACTION, {"incident": incident, "id": action_id, "kind": channel, "status": status,
                                "url": None, "detail": f"{event}: {headline}", "now": int(time.time())})
        results.append({"id": action_id, "status": status})
    return results

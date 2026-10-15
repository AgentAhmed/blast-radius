"""Chat notifications (Slack / Discord incoming webhooks) against a local fake webhook server. Needs FalkorDB."""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from blastradius import actions, db

TENANT = "test_actions"


class Hook(BaseHTTPRequestHandler):
    received: list = []
    fail = False

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        Hook.received.append((self.path, body))
        self.send_response(500 if Hook.fail else 204)
        self.end_headers()

    def log_message(self, *args):
        pass


@pytest.fixture(scope="module")
def server():
    srv = HTTPServer(("127.0.0.1", 0), Hook)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


@pytest.fixture(autouse=True)
def incident(monkeypatch, server):
    db.graph(TENANT).query("MERGE (:Incident {id: 'INC-1'})")
    Hook.received.clear()
    Hook.fail = False
    monkeypatch.setenv("SLACK_WEBHOOK_URL", f"{server}/slack")
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", f"{server}/discord")
    yield
    db.graph(TENANT).delete()


def notify(event="identified"):
    return actions.notify(TENANT, "INC-1", event, "INC-1 SEV2 root cause: payment", ["payment fails"],
                          "https://github.com/o/r/issues/1")


def test_posts_once_per_channel_in_each_format():
    first = notify()
    assert [r["status"] for r in first] == ["sent", "sent"]
    bodies = dict(Hook.received)
    assert bodies["/slack"]["text"].startswith("*INC-1 SEV2")
    assert "<https://github.com/o/r/issues/1|Tracking issue>" in bodies["/slack"]["text"]
    assert bodies["/discord"]["content"].startswith("**INC-1 SEV2")
    assert bodies["/discord"]["username"] == "Blast Radius"

    # Idempotent: the same event is not posted twice; a new event is.
    assert [r["status"] for r in notify()] == ["already_done", "already_done"]
    notify("resolved")
    assert len(Hook.received) == 4
    kinds = db.rows(db.graph(TENANT).query(
        "MATCH (:Incident {id: 'INC-1'})-[:HAS_ACTION]->(a:Action) RETURN a.kind AS kind, a.status AS status"))
    assert sorted(r["kind"] for r in kinds) == ["discord", "discord", "slack", "slack"]


def test_failure_is_recorded_without_the_secret_url(server):
    Hook.fail = True
    results = notify()
    assert all(r["status"].startswith("failed: HTTPError 500") for r in results)
    stored = db.rows(db.graph(TENANT).query("MATCH (a:Action) RETURN a.status AS status, a.url AS url"))
    assert all(server not in (r["status"] or "") and r["url"] is None for r in stored)
    # A failed post is retried next time.
    Hook.fail = False
    assert [r["status"] for r in notify()] == ["sent", "sent"]


def test_skipped_when_no_webhook_is_configured(monkeypatch):
    monkeypatch.delenv("SLACK_WEBHOOK_URL")
    monkeypatch.delenv("DISCORD_WEBHOOK_URL")
    assert notify() == []
    assert Hook.received == []

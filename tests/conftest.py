"""Tests must never act in the outside world: drop real credentials loaded from .env before every test."""
import pytest

OUTBOUND = ["GITHUB_TOKEN", "GITHUB_REPO", "SLACK_WEBHOOK_URL", "DISCORD_WEBHOOK_URL"]


@pytest.fixture(autouse=True)
def no_real_actions(monkeypatch):
    for name in OUTBOUND:
        monkeypatch.delenv(name, raising=False)

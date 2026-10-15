"""Integration tests: run against a live FalkorDB (docker compose up -d)."""
import pytest

from blastradius import db, queries
from blastradius.ingest import ingest

TENANT = "test_astroshop"
NOW = 1_800_000_000  # fixed clock so on-call and "recent" windows are reproducible


@pytest.fixture(scope="module", autouse=True)
def graph():
    ingest(TENANT, reset=True, now=NOW)
    yield
    db.graph(TENANT).delete()


def names(rows, key="component"):
    return [r[key] for r in rows]


def test_blast_radius_of_payment_reaches_the_user():
    result = queries.blast_radius("payment", TENANT)
    hops = {r["component"]: r["hops"] for r in result["dependents"]}
    assert hops == {"checkout": 1, "frontend": 2, "frontend-proxy": 3, "load-generator": 4}
    assert "checkout" in names(result["flows_at_risk"], "flow")


def test_blast_radius_follows_async_topic_edges():
    result = queries.blast_radius("orders", TENANT)
    assert {"checkout", "accounting", "fraud-detection"} <= set(names(result["dependents"]))


def test_leaf_consumer_has_no_dependents():
    assert queries.blast_radius("fraud-detection", TENANT)["dependents"] == []


@pytest.mark.parametrize("alerting, expected", [
    (["payment", "checkout", "frontend", "frontend-proxy"], "payment"),
    (["checkout", "frontend", "frontend-proxy"], "checkout"),        # deepest alerting service; agent then drills down with Q3
    (["cart", "checkout", "frontend"], "cart"),
    (["accounting", "fraud-detection"], "orders"),                   # shared Kafka topic, neither alerting service is the cause
    (["recommendation", "product-reviews", "frontend", "checkout"], "product-catalog"),
])
def test_root_cause_ranking(alerting, expected):
    top = queries.root_cause_candidates(alerting, TENANT, now=NOW)[0]
    assert top["component"] == expected


def test_root_cause_prefers_recent_change_between_equal_candidates():
    # checkout alerts alone: every dependency covers it; payment had a change 2h ago (planted).
    ranked = queries.root_cause_candidates(["checkout"], TENANT, now=NOW, limit=20)
    assert ranked[0]["component"] == "checkout"
    others = [r for r in ranked if not r["is_alerting"]]
    assert others[0]["component"] == "payment"


def test_recent_changes_near_checkout_include_planted_payment_change():
    rows = queries.recent_changes("checkout", TENANT, window_hours=24, now=NOW)
    payment = [r for r in rows if r["component"] == "payment"]
    assert payment and payment[0]["title"] == "Switch payment gateway client to v3 SDK"
    assert payment[0]["hops"] == 1 and payment[0]["author"] == "chen.w"


def test_who_to_page_returns_current_on_call_for_owner_team():
    rows = queries.who_to_page("payment", TENANT, now=NOW)
    assert len(rows) == 1
    assert rows[0]["team"] == "Payments"
    assert rows[0]["on_call"] in {"priya.n", "jonas.b", "chen.w"}
    assert rows[0]["on_call_until"] > NOW


def test_experts_are_ranked_by_weighted_changes():
    rows = queries.experts("payment", TENANT, now=NOW)
    assert rows and rows[0]["score"] >= rows[-1]["score"]
    assert "chen.w" in names(rows, "person")

"""Synthetic company data for a tenant: teams, people, changes, on-call, incidents, decisions.

Everything here is fictional (hackathon Rule 6). Generation is deterministic for a given seed,
and all timestamps are epoch seconds relative to `now`, so the data always looks "current".
"""
import random

from blastradius.data import topology

HOUR = 3600
DAY = 24 * HOUR

TEAMS = {
    "Storefront": ["frontend-proxy", "frontend", "image-provider", "ad", "recommendation", "product-reviews"],
    "Commerce": ["cart", "checkout", "product-catalog", "currency"],
    "Payments": ["payment", "fraud-detection", "accounting"],
    "Fulfillment": ["shipping", "quote", "email"],
    "Platform": ["flagd", "load-generator", "valkey-cart", "postgresql", "orders"],
}

# Fictional people: (handle, display name, team, role)
PEOPLE = [
    ("mira.k", "Mira Kovac", "Storefront", "lead"),
    ("tobi.a", "Tobi Adeyemi", "Storefront", "engineer"),
    ("lena.s", "Lena Sato", "Storefront", "engineer"),
    ("ravi.m", "Ravi Menon", "Commerce", "lead"),
    ("ines.f", "Ines Ferreira", "Commerce", "engineer"),
    ("omar.h", "Omar Haddad", "Commerce", "engineer"),
    ("priya.n", "Priya Nair", "Payments", "lead"),
    ("jonas.b", "Jonas Berg", "Payments", "engineer"),
    ("chen.w", "Chen Wei", "Payments", "engineer"),
    ("sara.l", "Sara Lindqvist", "Fulfillment", "lead"),
    ("diego.r", "Diego Ramos", "Fulfillment", "engineer"),
    ("amara.o", "Amara Okafor", "Platform", "lead"),
    ("felix.d", "Felix Dubois", "Platform", "engineer"),
    ("yuki.t", "Yuki Tanaka", "Platform", "engineer"),
]

# Components only one person has ever changed (bus factor = 1)
SOLE_MAINTAINER = {"quote": "diego.r"}

PR_TEMPLATES = [
    "Bump dependencies for {s}",
    "Add tracing attributes to {s}",
    "Refactor {s} request handling",
    "Tune timeouts in {s}",
    "Fix flaky test in {s}",
    "Improve error messages in {s}",
    "Add metrics for {s} latency",
    "Update {s} config defaults",
]


def _owner_team(component: str) -> str:
    return next(t for t, comps in TEAMS.items() if component in comps)


def generate(now: int, seed: int = 42) -> dict:
    rng = random.Random(seed)
    members = {t: [p[0] for p in PEOPLE if p[2] == t] for t in TEAMS}

    # --- On-call: weekly rotation per team, from 4 weeks ago to 2 weeks ahead -------------
    week = 7 * DAY
    start = now - (now % week) - 4 * week
    on_call = []
    for team, people in members.items():
        for i in range(6):
            on_call.append({
                "handle": people[i % len(people)], "team": team,
                "from": start + i * week, "to": start + (i + 1) * week,
            })

    # --- Pull requests + releases ---------------------------------------------------------
    prs, releases = [], []
    number = 1000
    version = {}
    components = [s[0] for s in topology.SERVICES]
    for comp in components:
        team = _owner_team(comp)
        for _ in range(rng.randint(3, 7)):
            number += 1
            outsider = rng.random() < 0.2
            pool = [p[0] for p in PEOPLE] if outsider else members[team]
            if comp in SOLE_MAINTAINER:   # planted knowledge risk for the bus-factor demo
                pool = [SOLE_MAINTAINER[comp]]
            merged = now - rng.randint(2 * DAY, 60 * DAY)
            prs.append({
                "number": number, "title": rng.choice(PR_TEMPLATES).format(s=comp),
                "service": comp, "author": rng.choice(pool), "merged_at": merged,
            })
    # Planted recent change for the demo: a risky payment change shipped ~2 hours ago,
    # authored by someone who is not the payment lead.
    number += 1
    prs.append({
        "number": number, "title": "Switch payment gateway client to v3 SDK",
        "service": "payment", "author": "chen.w", "merged_at": now - 2 * HOUR,
    })
    number += 1
    prs.append({
        "number": number, "title": "Cache product list in frontend for 60s",
        "service": "frontend", "author": "tobi.a", "merged_at": now - 5 * HOUR,
    })

    for pr in sorted(prs, key=lambda p: p["merged_at"]):
        minor = version.get(pr["service"], 0) + 1
        version[pr["service"]] = minor
        releases.append({
            "id": f"{pr['service']}@1.{minor}.0", "version": f"1.{minor}.0",
            "service": pr["service"], "deployed_at": pr["merged_at"] + 30 * 60,
            "pr": pr["number"],
        })

    # --- Historical incidents (episodic memory seed) -------------------------------------
    incidents = [
        _incident("INC-101", "Checkout failing: payment gateway errors", "SEV1", 40, 2,
                  "payment", ["checkout", "frontend", "frontend-proxy"], "priya.n",
                  "rollback", "Rolled back payment to the previous release; gateway client upgrade had a bad retry config.",
                  "A payment client upgrade changed retry behaviour. Payment calls failed and checkout returned 500s.",
                  now),
        _incident("INC-102", "Carts lost after valkey memory pressure", "SEV2", 33, 3,
                  "valkey-cart", ["cart", "frontend", "checkout"], "felix.d",
                  "config", "Raised valkey maxmemory and restarted cart pods.",
                  "Valkey hit maxmemory and evicted cart keys. Users saw empty carts and checkout failures.",
                  now),
        _incident("INC-103", "Order events delayed in Kafka", "SEV2", 27, 4,
                  "orders", ["accounting", "fraud-detection"], "amara.o",
                  "scale", "Scaled consumers and removed a poison message from the orders topic.",
                  "Consumer lag grew on the orders topic. Accounting and fraud checks ran hours behind.",
                  now),
        _incident("INC-104", "Product pages empty", "SEV1", 21, 1,
                  "product-catalog", ["frontend", "recommendation", "product-reviews", "checkout"], "ravi.m",
                  "flag-off", "Disabled the productCatalogFailure feature flag left on after a test.",
                  "A feature flag left on after a test made product-catalog fail; browse and checkout broke.",
                  now),
        _incident("INC-105", "Ad service CPU saturation", "SEV3", 16, 2,
                  "ad", ["frontend"], "lena.s",
                  "restart", "Restarted ad service and opened a ticket to fix the hot loop.",
                  "Ad service pinned CPU; frontend latency rose but requests succeeded.",
                  now),
        _incident("INC-106", "Order confirmation emails slow", "SEV3", 12, 5,
                  "email", ["checkout"], "diego.r",
                  "restart", "Restarted email service; memory leak fix scheduled.",
                  "Email service leaked memory; checkout latency rose while waiting on confirmation emails.",
                  now),
        _incident("INC-107", "Recommendations timing out", "SEV3", 8, 2,
                  "recommendation", ["frontend"], "mira.k",
                  "flag-off", "Disabled recommendationCacheFailure flag and warmed the cache.",
                  "Recommendation cache failed; every request hit product-catalog and timed out.",
                  now),
        _incident("INC-108", "Payment unreachable from checkout", "SEV1", 5, 1,
                  "payment", ["checkout", "frontend", "frontend-proxy"], "jonas.b",
                  "config", "Restored the network policy that allowed checkout to reach payment.",
                  "A network policy change blocked checkout from reaching payment; all orders failed.",
                  now),
    ]

    # --- Design decisions (ADRs) -------------------------------------------------------
    decisions = [
        {"id": "ADR-001", "title": "Publish orders to Kafka instead of calling accounting synchronously",
         "made_by": "amara.o", "date": now - 200 * DAY, "status": "accepted",
         "rationale": "Checkout must not fail or slow down when accounting or fraud checks are slow. "
                      "Async events decouple them; the cost is eventual consistency and consumer lag to monitor.",
         "affects": ["checkout", "orders", "accounting", "fraud-detection"]},
        {"id": "ADR-002", "title": "Store carts in Valkey",
         "made_by": "ravi.m", "date": now - 180 * DAY, "status": "accepted",
         "rationale": "Carts are short-lived, read-heavy key-value data. Valkey gives low latency; "
                      "we accept that carts can be evicted under memory pressure.",
         "affects": ["cart", "valkey-cart"]},
        {"id": "ADR-003", "title": "Use flagd (OpenFeature) for feature flags",
         "made_by": "amara.o", "date": now - 150 * DAY, "status": "accepted",
         "rationale": "Vendor-neutral flags let us switch behaviour, including failure drills, without deploys.",
         "affects": ["flagd", "frontend-proxy"]},
        {"id": "ADR-004", "title": "Use gRPC for internal service calls",
         "made_by": "ravi.m", "date": now - 240 * DAY, "status": "accepted",
         "rationale": "Typed contracts across many languages and lower overhead than JSON over HTTP.",
         "affects": ["frontend", "checkout", "cart", "payment", "product-catalog", "currency"]},
        {"id": "ADR-005", "title": "Share one PostgreSQL instance between accounting and product reviews",
         "made_by": "felix.d", "date": now - 90 * DAY, "status": "accepted",
         "rationale": "Saves cost at current scale. Risk: one noisy workload can slow the other; revisit at 10x traffic.",
         "affects": ["postgresql", "accounting", "product-reviews"]},
    ]

    runbooks = [
        {"id": "RB-payment", "service": "payment", "title": "Payment errors",
         "steps": "1. Check recent payment releases. 2. Check paymentFailure/paymentUnreachable flags. "
                  "3. Check network policy checkout->payment. 4. Roll back last release if errors began after deploy."},
        {"id": "RB-cart", "service": "cart", "title": "Cart failures",
         "steps": "1. Check valkey-cart memory and evictions. 2. Check cartFailure flag. 3. Restart cart if valkey is healthy."},
        {"id": "RB-orders", "service": "orders", "title": "Kafka consumer lag",
         "steps": "1. Check consumer lag per group. 2. Look for poison messages. 3. Scale consumers."},
        {"id": "RB-product-catalog", "service": "product-catalog", "title": "Catalog failures",
         "steps": "1. Check productCatalogFailure flag. 2. Check recent catalog releases. 3. Roll back."},
        {"id": "RB-checkout", "service": "checkout", "title": "Checkout errors",
         "steps": "1. Find which downstream call fails (payment, cart, shipping, currency). 2. Follow that service's runbook."},
    ]

    return {
        "teams": [{"name": t, "owns": comps} for t, comps in TEAMS.items()],
        "people": [{"handle": h, "name": n, "team": t, "role": r} for h, n, t, r in PEOPLE],
        "on_call": on_call,
        "prs": prs,
        "releases": releases,
        "incidents": incidents,
        "decisions": decisions,
        "runbooks": runbooks,
    }


def _incident(iid, title, sev, days_ago, hours_long, root, impacted, handler,
              action_kind, action_detail, summary, now):
    opened = now - days_ago * DAY
    return {
        "id": iid, "title": title, "severity": sev, "status": "resolved",
        "opened_at": opened, "resolved_at": opened + hours_long * HOUR,
        "root_cause": root, "impacted": impacted, "handled_by": handler,
        "summary": summary,
        "action": {"id": f"{iid}-A1", "kind": action_kind, "detail": action_detail,
                   "at": opened + hours_long * HOUR},
        "postmortem": {"id": f"PM-{iid}", "title": f"Postmortem: {title}",
                       "body": f"{summary} Resolution: {action_detail}"},
    }

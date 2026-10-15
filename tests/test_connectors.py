"""Connectors without network: repository files -> system model, and alert payloads -> alerts."""
from blastradius.connectors import repo_model as rm
from blastradius.incidents import match_component, parse_alerts

BASE = """
services:
  frontend:
    build: {context: ./, dockerfile: src/frontend/Dockerfile}
    ports: ["8080:8080"]
    environment:
      - CART_ADDR=cart:7070
      - API_URL=http://checkout:5050/api
      - OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4317
  cart:
    build: ./src/cart
    environment: {VALKEY_ADDR: "valkey-cart:6379"}
    depends_on: [valkey-cart]
  checkout:
    build: {context: ./src/checkout}
    depends_on: {cart: {condition: service_started}}
  valkey-cart:
    image: valkey/valkey:8
  load-generator:
    build: ./src/load-generator
    environment: [LOCUST_HOST=http://frontend:8080]
  otel-collector:
    image: otel/opentelemetry-collector-contrib
"""
OVERRIDE = """
services:
  checkout:
    environment: [KAFKA_ADDR=kafka:9092]
  kafka:
    image: apache/kafka:3.7
  accounting:
    build: ./src/accounting
    depends_on: [kafka]
"""


def test_compose_services_dependencies_and_kinds():
    m = rm.from_compose([BASE, OVERRIDE])
    assert "otel-collector" not in m.components                         # observability is not the system
    assert m.components["valkey-cart"]["kind"] == "datastore"
    assert m.components["kafka"]["kind"] == "topic"
    assert m.components["frontend"]["paths"] == ["src/frontend"]         # from dockerfile directory
    assert m.components["cart"]["paths"] == ["src/cart"]
    assert {("frontend", "cart", "CALLS"), ("frontend", "checkout", "CALLS"),
            ("cart", "valkey-cart", "READS_WRITES"), ("checkout", "cart", "CALLS"),
            ("checkout", "kafka", "PUBLISHES"), ("accounting", "kafka", "PUBLISHES")} <= m.deps
    assert ("frontend", "otel-collector", "CALLS") not in m.deps


def test_entry_point_and_tiers():
    m = rm.from_compose([BASE, OVERRIDE])
    assert m.entry == "frontend"                     # exposed root; the load generator is not an entry
    assert m.components["frontend"]["tier"] == "edge"
    assert m.components["valkey-cart"]["tier"] == "core"
    assert m.components["cart"]["tier"] == "core"    # two dependents


def test_compose_file_discovery_skips_tests():
    files = {"compose.yaml", "compose.full.yaml", "compose.tests.yaml", "src/x/compose.yaml", "README.md"}
    assert rm.compose_files(files) == ["compose.yaml", "compose.full.yaml"]


def test_catalog_file():
    m, teams = rm.from_catalog("""
entrypoint: web
components:
  - {name: web, paths: [apps/web], depends_on: [api], flows: [checkout]}
  - {name: api, paths: [services/api], depends_on: [db]}
  - {name: db, kind: datastore}
teams:
  - {name: Core, owns: [api, db], members: [alice]}
""")
    assert m.entry == "web" and ("api", "db", "READS_WRITES") in m.deps and m.flows == {"checkout": ["web"]}
    assert teams == [{"name": "Core", "owns": ["api", "db"], "members": ["alice"]}]


def test_codeowners_last_match_wins_and_teams():
    m = rm.from_compose([BASE])
    rules = rm.parse_codeowners("""
# global owners
*                 @org/platform
/src/cart/        @org/commerce   # cart team
src/frontend      @alice bob@example.com
""")
    owners = rm.owners_by_component(m, rules)
    assert owners["cart"] == ["@org/commerce"]
    assert owners["checkout"] == ["@org/platform"]
    teams = {t["name"]: t for t in rm.teams_from_owners(owners, real_names=True)}
    assert teams["commerce"]["owns"] == ["cart"]
    assert teams["frontend-owners"]["members"] == ["alice", "bob"]


def test_pseudonyms_are_stable_unique_and_hide_the_login():
    a1, a2, b = rm.person_id("Alice", False), rm.person_id("@alice", False), rm.person_id("bob", False)
    assert a1 == a2 and a1 != b and a1.startswith("dev-") and "alice" not in a1
    assert rm.person_id("@Alice", True) == "alice"


def test_files_map_to_the_most_specific_component():
    m = rm.SystemModel(components={"api": {"paths": ["services/api"]}, "api-admin": {"paths": ["services/api/admin"]}})
    assert rm.components_for_file(m, "services/api/admin/main.go") == ["api-admin"]
    assert rm.components_for_file(m, "services/api/main.go") == ["api"]
    assert rm.components_for_file(m, "docs/readme.md") == []


def test_adr_parsing():
    adr = rm.parse_adr("docs/adr/0003-kafka.md", "# Use Kafka for orders\n\nStatus: Accepted\nDate: 2024-05-01\n\n"
                       "Checkout publishes to kafka so accounting is decoupled.", ["checkout", "kafka", "cart"], 3)
    assert adr["id"] == "ADR-003" and adr["title"] == "Use Kafka for orders" and adr["status"] == "accepted"
    assert adr["affects"] == ["checkout", "kafka"] and adr["date"]


def test_alert_label_matching():
    names = ["cart", "checkout", "payment", "valkey-cart"]
    assert match_component("payment-service", names) == "payment"
    assert match_component("default/cart-7d9f8b-x2x9q", names) == "cart"
    assert match_component("valkey-cart", names) == "valkey-cart"
    assert match_component("billing", names) is None


def test_alertmanager_and_generic_payloads():
    names = ["cart", "payment"]
    am = {"version": "4", "status": "firing", "alerts": [
        {"status": "firing", "labels": {"alertname": "HighErrorRate", "service": "payment", "severity": "critical"},
         "annotations": {"summary": "5xx > 5%"}, "startsAt": "2026-10-15T10:00:00Z"},
        {"status": "resolved", "labels": {"alertname": "HighLatency", "pod": "cart-abc12-xyz"}},
        {"status": "firing", "labels": {"alertname": "Down", "job": "billing"}},
    ]}
    firing, resolved, unmatched = parse_alerts(am, names)
    assert firing[0]["component"] == "payment" and firing[0]["signal"] == "high_error_rate"
    assert firing[0]["severity"] == "critical" and firing[0]["fired_at"] > 0
    assert resolved[0]["component"] == "cart" and unmatched == ["billing"]
    firing, _, _ = parse_alerts({"component": "cart", "signal": "error_rate", "value": 0.4}, names)
    assert firing[0]["component"] == "cart" and firing[0]["value"] == 0.4

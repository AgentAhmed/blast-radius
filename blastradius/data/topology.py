"""System topology of the OpenTelemetry Demo ("Astronomy Shop"), Apache-2.0.

Derived from the public architecture docs: https://opentelemetry.io/docs/demo/architecture/
Observability components (collector, Jaeger, Grafana, Prometheus) are left out: they are
not part of the shop's request path.

Every dependency edge points from the dependent component to what it depends on.
"""

# (name, kind, language, tier)
#   tier: edge = user-facing entry, core = on the money path, support = everything else
SERVICES = [
    ("frontend-proxy", "service", "C++ (Envoy)", "edge"),
    ("frontend", "service", "TypeScript", "edge"),
    ("image-provider", "service", "nginx", "support"),
    ("ad", "service", "Java", "support"),
    ("cart", "service", ".NET", "core"),
    ("checkout", "service", "Go", "core"),
    ("currency", "service", "C++", "core"),
    ("email", "service", "Ruby", "support"),
    ("payment", "service", "JavaScript", "core"),
    ("product-catalog", "service", "Go", "core"),
    ("product-reviews", "service", "Python", "support"),
    ("recommendation", "service", "Python", "support"),
    ("shipping", "service", "Rust", "core"),
    ("quote", "service", "PHP", "support"),
    ("accounting", "service", ".NET", "support"),
    ("fraud-detection", "service", "Kotlin", "support"),
    ("flagd", "service", "Go", "support"),
    ("load-generator", "service", "Python (Locust)", "support"),
]

# (name, kind)
DATASTORES = [
    ("valkey-cart", "valkey"),
    ("postgresql", "postgres"),
]

TOPICS = [
    "orders",  # Kafka topic
]

# Synchronous calls: caller -> callee
CALLS = [
    ("load-generator", "frontend-proxy", "http"),
    ("frontend-proxy", "frontend", "http"),
    ("frontend-proxy", "image-provider", "http"),
    ("frontend-proxy", "flagd", "http"),
    ("frontend", "ad", "grpc"),
    ("frontend", "cart", "grpc"),
    ("frontend", "checkout", "grpc"),
    ("frontend", "currency", "grpc"),
    ("frontend", "product-catalog", "grpc"),
    ("frontend", "product-reviews", "grpc"),
    ("frontend", "recommendation", "grpc"),
    ("frontend", "shipping", "http"),
    ("checkout", "cart", "grpc"),
    ("checkout", "currency", "grpc"),
    ("checkout", "email", "http"),
    ("checkout", "payment", "grpc"),
    ("checkout", "product-catalog", "grpc"),
    ("checkout", "shipping", "http"),
    ("recommendation", "product-catalog", "grpc"),
    ("product-reviews", "product-catalog", "grpc"),
    ("shipping", "quote", "http"),
]

# service -> datastore
READS_WRITES = [
    ("cart", "valkey-cart"),
    ("accounting", "postgresql"),
    ("product-reviews", "postgresql"),
]

# service -> topic
PUBLISHES = [("checkout", "orders")]
CONSUMES = [("accounting", "orders"), ("fraud-detection", "orders")]

# Business view: which user flows each component serves directly
USER_FLOWS = {
    "browse": ["frontend-proxy", "frontend", "product-catalog", "currency", "image-provider",
               "ad", "recommendation", "product-reviews"],
    "cart": ["frontend-proxy", "frontend", "cart"],
    "checkout": ["frontend-proxy", "frontend", "checkout", "payment", "shipping", "quote",
                 "email", "currency", "cart"],
    "order-processing": ["accounting", "fraud-detection"],
}

# Built-in failure flags of the demo (served by flagd). Used by the incident simulator.
# Names follow the demo's flagd config; verify against the version you run.
FAULT_FLAGS = {
    "paymentFailure": "payment",
    "paymentUnreachable": "payment",
    "cartFailure": "cart",
    "adFailure": "ad",
    "productCatalogFailure": "product-catalog",
    "recommendationCacheFailure": "recommendation",
    "kafkaQueueProblems": "orders",
    "emailMemoryLeak": "email",
}

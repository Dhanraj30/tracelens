"""Deterministic, idempotent seed for the TraceLens demo graph.

Fixed IDs and fixed timestamps (integer epoch seconds) relative to BASE_DAY so every run
produces the identical graph. `reset_and_seed` deletes ONLY the demo graph (by name) and
rebuilds it. Ground truth for the scenarios lives in tests/scenarios.yaml — not here and
not in the agent.

Scenario timeline (all times on BASE_DAY, UTC):
  13:20 cart deploy (dep-cart-1320)              -> candidate, ruled out by origin check
  13:52 payments-api deploy (dep-pay-1352)       -> cuts DB pool 50 -> 5   [real cause]
  13:58 payments-api PoolTimeoutError begins
  14:01 checkout UpstreamTimeout begins          -> BEFORE the checkout deploy
  14:02 gateway HTTP502 begins
  14:05 checkout deploy (dep-chk-1405)           -> the obvious suspect, ruled out by time
  14:08 refunds-worker UpstreamTimeout begins
  14:10 incident inc-1042 opened (Scenario A)
  14:25 incident inc-1043 opened (Scenario B)
  15:12 search EmptyResultSet begins
  15:30 incident inc-1044 opened (Scenario C)

  +7 days (Scenario D - learning from approvals):
  10:20 auth deploy (dep-auth-d)                 -> candidate, ruled out by origin check
  10:40 payments-api deploy (dep-pay-d)          -> pool tuned again (5 -> 8), still too small   [real cause]
  10:47 payments-api PoolTimeoutError begins
  10:50 checkout UpstreamTimeout begins
  11:00 incident inc-1051 opened                 -> history check finds TraceLens's own Resolution of inc-1042 (if approved)
"""
from __future__ import annotations

import calendar
from typing import Any

from .db import Graph

BASE_DAY = calendar.timegm((2026, 10, 14, 0, 0, 0))  # Wed 14 Oct 2026 00:00:00 UTC
DAY = 86400


def t(hh: int, mm: int, day_offset: int = 0) -> int:
    """Epoch seconds for HH:MM on BASE_DAY (+ day_offset days)."""
    return BASE_DAY + day_offset * DAY + hh * 3600 + mm * 60


# --------------------------------------------------------------------------------------
# Services and dependency edges. A DEPENDS_ON B means A calls / needs B.
# weight: sync = 1, async = 2 (used by algo.SPpaths as weightProp).
# --------------------------------------------------------------------------------------
SERVICES: list[dict[str, Any]] = [
    {"name": "gateway", "tier": "edge", "team": "platform"},
    {"name": "checkout", "tier": "core", "team": "commerce"},
    {"name": "cart", "tier": "core", "team": "commerce"},
    {"name": "payments-api", "tier": "core", "team": "payments"},
    {"name": "fraud-check", "tier": "core", "team": "risk"},
    {"name": "inventory", "tier": "core", "team": "supply"},
    {"name": "auth", "tier": "core", "team": "identity"},
    {"name": "refunds-worker", "tier": "worker", "team": "payments"},
    {"name": "search", "tier": "core", "team": "discovery"},
    {"name": "notification", "tier": "worker", "team": "platform"},
    {"name": "postgres-payments", "tier": "datastore", "team": "payments"},
    {"name": "redis-sessions", "tier": "datastore", "team": "platform"},
    {"name": "kafka", "tier": "infra", "team": "platform"},
    {"name": "vendor-search-index", "tier": "external", "team": "third-party", "external": True},
]

DEPENDS_ON: list[tuple[str, str, str]] = [  # (from, to, kind)
    ("gateway", "checkout", "sync"),
    ("gateway", "cart", "sync"),
    ("gateway", "search", "sync"),
    ("gateway", "auth", "sync"),
    ("checkout", "cart", "sync"),
    ("checkout", "payments-api", "sync"),
    ("checkout", "inventory", "sync"),
    ("checkout", "auth", "sync"),
    ("checkout", "notification", "async"),
    ("cart", "redis-sessions", "sync"),
    ("payments-api", "postgres-payments", "sync"),
    ("payments-api", "fraud-check", "sync"),
    ("payments-api", "kafka", "async"),
    ("fraud-check", "postgres-payments", "sync"),
    ("refunds-worker", "payments-api", "sync"),
    ("refunds-worker", "kafka", "async"),
    ("inventory", "kafka", "async"),
    ("auth", "redis-sessions", "sync"),
    ("search", "vendor-search-index", "sync"),
    ("notification", "kafka", "async"),
]

# --------------------------------------------------------------------------------------
# Deployments and code changes
# --------------------------------------------------------------------------------------
DEPLOYMENTS: list[dict[str, Any]] = [
    # --- Scenario A window (13:10-14:10) ---
    {"id": "dep-cart-1320", "service": "cart", "at": t(13, 20), "version": "v3.8.2", "author": "lena",
     "changes": [{"id": "cc-cart-qty", "summary": "Add cart item quantity validation", "files": ["cart/validate.py"], "risk": "low"}]},
    {"id": "dep-pay-1352", "service": "payments-api", "at": t(13, 52), "version": "v2.14.0", "author": "priya",
     "changes": [{"id": "cc-pool-size", "summary": "Reduce Postgres connection pool max_size from 50 to 5 to lower idle connections",
                  "files": ["payments/db/pool.py", "payments/settings.py"], "risk": "high"}]},
    {"id": "dep-chk-1405", "service": "checkout", "at": t(14, 5), "version": "v5.3.1", "author": "marco",
     "changes": [{"id": "cc-checkout-copy", "summary": "Update checkout button copy and analytics event names",
                  "files": ["checkout/web/PayButton.tsx"], "risk": "low"}]},
    # --- Scenario D window (+7 days, 10:00-11:00) ---
    {"id": "dep-auth-d", "service": "auth", "at": t(10, 20, 7), "version": "v1.23.0", "author": "sam",
     "changes": [{"id": "cc-auth-samesite", "summary": "Set session cookie SameSite=Lax", "files": ["auth/cookies.py"], "risk": "low"}]},
    {"id": "dep-pay-d", "service": "payments-api", "at": t(10, 40, 7), "version": "v2.15.0", "author": "priya",
     "changes": [{"id": "cc-pool-size-2", "summary": "Set Postgres connection pool max_size to 8 (was 50 before the rollback)",
                  "files": ["payments/db/pool.py"], "risk": "high"}]},
    # --- same day, outside windows ---
    {"id": "dep-auth-1100", "service": "auth", "at": t(11, 0), "version": "v1.22.0", "author": "sam",
     "changes": [{"id": "cc-auth-jwt", "summary": "Rotate JWT signing key", "files": ["auth/keys.py"], "risk": "medium"}]},
    {"id": "dep-inv-0930", "service": "inventory", "at": t(9, 30), "version": "v4.1.0", "author": "wei",
     "changes": [{"id": "cc-inv-cache", "summary": "Increase stock cache TTL to 60s", "files": ["inventory/cache.py"], "risk": "medium"}]},
    {"id": "dep-gw-0800", "service": "gateway", "at": t(8, 0), "version": "v9.0.3", "author": "sam",
     "changes": [{"id": "cc-gw-headers", "summary": "Forward X-Request-ID header", "files": ["gateway/middleware.go"], "risk": "low"}]},
    # --- history (previous weeks) ---
    {"id": "dep-pay-h1", "service": "payments-api", "at": t(10, 15, -21), "version": "v2.9.3", "author": "priya",
     "changes": [{"id": "cc-pool-tune", "summary": "Tune Postgres pool: lower max_size and pool_timeout", "files": ["payments/db/pool.py"], "risk": "high"}]},
    {"id": "dep-pay-h2", "service": "payments-api", "at": t(16, 40, -21), "version": "v2.9.4", "author": "priya",
     "changes": [{"id": "cc-pool-restore", "summary": "Restore pool max_size to 50 (rollback of v2.9.3)", "files": ["payments/db/pool.py"], "risk": "low"}]},
    {"id": "dep-auth-h1", "service": "auth", "at": t(12, 0, -14), "version": "v1.20.0", "author": "sam",
     "changes": [{"id": "cc-auth-redis", "summary": "Point session store at new Redis cluster", "files": ["auth/redis.py"], "risk": "medium"}]},
    {"id": "dep-notif-h1", "service": "notification", "at": t(9, 0, -10), "version": "v0.9.0", "author": "wei",
     "changes": [{"id": "cc-notif-batch", "summary": "Batch Kafka consumer commits", "files": ["notification/consumer.py"], "risk": "medium"}]},
    {"id": "dep-inv-h1", "service": "inventory", "at": t(14, 0, -7), "version": "v4.0.0", "author": "wei",
     "changes": [{"id": "cc-inv-stale", "summary": "Serve stock counts from cache on DB timeout", "files": ["inventory/stock.py"], "risk": "high"}]},
    {"id": "dep-inv-h2", "service": "inventory", "at": t(18, 30, -7), "version": "v4.0.1", "author": "wei",
     "changes": [{"id": "cc-inv-stale-revert", "summary": "Revert cache-on-timeout behaviour", "files": ["inventory/stock.py"], "risk": "low"}]},
    {"id": "dep-cart-h1", "service": "cart", "at": t(11, 0, -5), "version": "v3.8.0", "author": "lena",
     "changes": [{"id": "cc-cart-merge", "summary": "Merge guest cart on login", "files": ["cart/merge.py"], "risk": "medium"}]},
    {"id": "dep-search-h1", "service": "search", "at": t(10, 0, -3), "version": "v7.2.0", "author": "ana",
     "changes": [{"id": "cc-search-rank", "summary": "New ranking weights", "files": ["search/rank.py"], "risk": "medium"}]},
    {"id": "dep-chk-h1", "service": "checkout", "at": t(15, 0, -2), "version": "v5.3.0", "author": "marco",
     "changes": [{"id": "cc-chk-coupons", "summary": "Coupon stacking rules", "files": ["checkout/coupons.py"], "risk": "medium"}]},
    {"id": "dep-gw-h1", "service": "gateway", "at": t(7, 30, -1), "version": "v9.0.2", "author": "sam",
     "changes": [{"id": "cc-gw-tls", "summary": "TLS 1.3 only", "files": ["gateway/tls.go"], "risk": "medium"}]},
]

# --------------------------------------------------------------------------------------
# Error events. Each has a signature key; ErrorSignature is a SHARED node so that
# "similar incidents" is a traversal, not fuzzy text matching.
# --------------------------------------------------------------------------------------
ERRORS: list[dict[str, Any]] = [
    # Scenario A
    {"id": "err-pay-1358", "service": "payments-api", "sig": "PoolTimeoutError", "at": t(13, 58), "count": 42,
     "message": "PoolTimeoutError: QueuePool limit of size 5 overflow 0 reached, connection timed out"},
    {"id": "err-pay-1403", "service": "payments-api", "sig": "PoolTimeoutError", "at": t(14, 3), "count": 318,
     "message": "PoolTimeoutError: QueuePool limit of size 5 overflow 0 reached, connection timed out"},
    {"id": "err-chk-1401", "service": "checkout", "sig": "UpstreamTimeout", "at": t(14, 1), "count": 27,
     "message": "UpstreamTimeout: payments-api did not respond within 3000ms (POST /v1/charges)"},
    {"id": "err-chk-1406", "service": "checkout", "sig": "UpstreamTimeout", "at": t(14, 6), "count": 194,
     "message": "UpstreamTimeout: payments-api did not respond within 3000ms (POST /v1/charges)"},
    {"id": "err-gw-1402", "service": "gateway", "sig": "HTTP502", "at": t(14, 2), "count": 61,
     "message": "502 Bad Gateway from upstream checkout"},
    # Scenario B
    {"id": "err-ref-1408", "service": "refunds-worker", "sig": "UpstreamTimeout", "at": t(14, 8), "count": 15,
     "message": "UpstreamTimeout: payments-api did not respond within 5000ms (POST /v1/refunds)"},
    {"id": "err-ref-1420", "service": "refunds-worker", "sig": "UpstreamTimeout", "at": t(14, 20), "count": 88,
     "message": "UpstreamTimeout: payments-api did not respond within 5000ms (POST /v1/refunds)"},
    # Scenario C
    {"id": "err-srch-1512", "service": "search", "sig": "EmptyResultSet", "at": t(15, 12), "count": 540,
     "message": "EmptyResultSet: vendor-search-index returned 0 hits for 100% of queries"},
    # Scenario D (+7 days)
    {"id": "err-pay-d", "service": "payments-api", "sig": "PoolTimeoutError", "at": t(10, 47, 7), "count": 58,
     "message": "PoolTimeoutError: QueuePool limit of size 8 overflow 0 reached, connection timed out"},
    {"id": "err-chk-d", "service": "checkout", "sig": "UpstreamTimeout", "at": t(10, 50, 7), "count": 41,
     "message": "UpstreamTimeout: payments-api did not respond within 3000ms (POST /v1/charges)"},
    # History
    {"id": "err-pay-h1", "service": "payments-api", "sig": "PoolTimeoutError", "at": t(10, 31, -21), "count": 260,
     "message": "PoolTimeoutError: QueuePool limit of size 10 overflow 0 reached, connection timed out"},
    {"id": "err-auth-h1", "service": "auth", "sig": "RedisConnectionRefused", "at": t(12, 9, -14), "count": 900,
     "message": "ConnectionRefusedError: redis-sessions:6379"},
    {"id": "err-notif-h1", "service": "notification", "sig": "ConsumerLag", "at": t(9, 40, -10), "count": 1,
     "message": "Kafka consumer lag > 50000 messages on topic order-events"},
    {"id": "err-inv-h1", "service": "inventory", "sig": "StaleStock", "at": t(14, 25, -7), "count": 73,
     "message": "StaleStock: served cached stock count older than 10 minutes"},
]

RUNBOOKS: list[dict[str, Any]] = [
    {"id": "rb-pool-sizing", "title": "Payments DB connection pool sizing", "service": "payments-api",
     "steps": ["Confirm PoolTimeoutError rate in payments-api logs",
               "Compare pool max_size in the current deploy vs previous",
               "Roll back the deployment that changed pool settings",
               "Verify postgres-payments active connections < 80% of max_connections",
               "Re-run checkout smoke test"]},
    {"id": "rb-redis-failover", "title": "Redis sessions failover", "service": "redis-sessions",
     "steps": ["Check redis-sessions primary health", "Promote replica", "Restart auth pods"]},
    {"id": "rb-kafka-rebalance", "title": "Kafka consumer group rebalance", "service": "kafka",
     "steps": ["Describe consumer group lag", "Scale consumer replicas", "Trigger rebalance"]},
    {"id": "rb-inventory-cache", "title": "Inventory cache invalidation", "service": "inventory",
     "steps": ["Flush stock cache", "Verify stock counts against DB", "Roll back if mismatch persists"]},
]

INCIDENTS: list[dict[str, Any]] = [
    # Scenario A - main demo
    {"id": "inc-1042", "title": "Checkout requests are failing", "status": "open", "opened_at": t(14, 10),
     "symptom": "Checkout error rate 38%, p99 latency > 5s", "service": "checkout",
     "observed": ["err-chk-1401", "err-chk-1406"]},
    # Scenario B - memory
    {"id": "inc-1043", "title": "Refunds failing", "status": "open", "opened_at": t(14, 25),
     "symptom": "Refund jobs failing with timeouts; backlog growing", "service": "refunds-worker",
     "observed": ["err-ref-1408", "err-ref-1420"]},
    # Scenario C - incomplete evidence
    {"id": "inc-1044", "title": "Search results are empty", "status": "open", "opened_at": t(15, 30),
     "symptom": "Search returns 0 results for all queries", "service": "search",
     "observed": ["err-srch-1512"]},
    # Scenario D - a week later, same shape; history should include TraceLens's own resolution of inc-1042 once approved
    {"id": "inc-1051", "title": "Checkout failing again after pool change", "status": "open", "opened_at": t(11, 0, 7),
     "symptom": "Checkout error rate 21%, charge timeouts", "service": "checkout",
     "observed": ["err-chk-d"]},
    # History (resolved)
    {"id": "inc-0877", "title": "Payments latency spike after pool tuning", "status": "resolved", "opened_at": t(10, 40, -21),
     "symptom": "Charge API p99 > 8s, pool timeouts", "service": "payments-api", "observed": ["err-pay-h1"],
     "resolution": {"id": "res-0877", "at": t(16, 45, -21), "runbook": "rb-pool-sizing", "reverted": "dep-pay-h1",
                    "summary": "Rolled back payments-api v2.9.3 (pool tuning) and restored pool max_size to 50 per runbook"}},
    {"id": "inc-0901", "title": "Login failures", "status": "resolved", "opened_at": t(12, 15, -14),
     "symptom": "Users cannot log in; 500s from auth", "service": "auth", "observed": ["err-auth-h1"],
     "resolution": {"id": "res-0901", "at": t(13, 5, -14), "runbook": "rb-redis-failover", "reverted": None,
                    "summary": "Promoted redis-sessions replica; restarted auth"}},
    {"id": "inc-0933", "title": "Order notifications delayed", "status": "resolved", "opened_at": t(9, 50, -10),
     "symptom": "Order confirmation emails delayed > 30 min", "service": "notification", "observed": ["err-notif-h1"],
     "resolution": {"id": "res-0933", "at": t(11, 0, -10), "runbook": "rb-kafka-rebalance", "reverted": None,
                    "summary": "Scaled notification consumers 2 -> 6 and rebalanced"}},
    {"id": "inc-0960", "title": "Inventory oversell", "status": "resolved", "opened_at": t(14, 30, -7),
     "symptom": "Orders accepted for out-of-stock items", "service": "inventory", "observed": ["err-inv-h1"],
     "resolution": {"id": "res-0960", "at": t(18, 40, -7), "runbook": "rb-inventory-cache", "reverted": "dep-inv-h1",
                    "summary": "Reverted inventory v4.0.0 and flushed stock cache"}},
]

INDEXES: list[tuple[str, str]] = [
    ("Service", "name"), ("Incident", "id"), ("Deployment", "id"), ("CodeChange", "id"),
    ("ErrorEvent", "id"), ("ErrorSignature", "key"), ("Runbook", "id"), ("Investigation", "id"),
    ("Hypothesis", "id"), ("Action", "id"),
]


def seed(g: Graph) -> dict[str, int]:
    """Create the demo graph. Idempotent via MERGE on IDs. Returns counts."""
    for label, prop in INDEXES:
        g.ensure_index(label, prop)

    g.write(
        "UNWIND $rows AS r MERGE (s:Service {name: r.name}) "
        "SET s.tier = r.tier, s.team = r.team, s.external = coalesce(r.external, false)",
        {"rows": SERVICES},
    )
    g.write(
        "UNWIND $rows AS r MATCH (a:Service {name: r.src}), (b:Service {name: r.dst}) "
        "MERGE (a)-[e:DEPENDS_ON]->(b) SET e.kind = r.kind, e.weight = r.weight",
        {"rows": [{"src": a, "dst": b, "kind": k, "weight": 1 if k == "sync" else 2} for a, b, k in DEPENDS_ON]},
    )
    g.write(
        "UNWIND $rows AS r MATCH (s:Service {name: r.service}) "
        "MERGE (d:Deployment {id: r.id}) SET d.at = r.at, d.version = r.version, d.author = r.author "
        "MERGE (d)-[:DEPLOYED_TO]->(s)",
        {"rows": [{k: v for k, v in d.items() if k != "changes"} for d in DEPLOYMENTS]},
    )
    g.write(
        "UNWIND $rows AS r MATCH (d:Deployment {id: r.dep}) "
        "MERGE (c:CodeChange {id: r.id}) SET c.summary = r.summary, c.files = r.files, c.risk = r.risk "
        "MERGE (d)-[:INCLUDES]->(c)",
        {"rows": [dict(c, dep=d["id"]) for d in DEPLOYMENTS for c in d["changes"]]},
    )
    g.write(
        "UNWIND $rows AS r MATCH (s:Service {name: r.service}) "
        "MERGE (e:ErrorEvent {id: r.id}) SET e.at = r.at, e.message = r.message, e.count = r.count "
        "MERGE (e)-[:EMITTED_BY]->(s) "
        "MERGE (sig:ErrorSignature {key: r.sig}) "
        "MERGE (e)-[:HAS_SIGNATURE]->(sig)",
        {"rows": ERRORS},
    )
    g.write(
        "UNWIND $rows AS r MATCH (s:Service {name: r.service}) "
        "MERGE (rb:Runbook {id: r.id}) SET rb.title = r.title, rb.steps = r.steps "
        "MERGE (rb)-[:APPLIES_TO]->(s)",
        {"rows": RUNBOOKS},
    )
    g.write(
        "UNWIND $rows AS r MATCH (s:Service {name: r.service}) "
        "MERGE (i:Incident {id: r.id}) SET i.title = r.title, i.status = r.status, i.opened_at = r.opened_at, i.symptom = r.symptom "
        "MERGE (i)-[:AFFECTS]->(s) "
        "WITH i, r UNWIND r.observed AS eid MATCH (e:ErrorEvent {id: eid}) MERGE (i)-[:OBSERVED]->(e)",
        {"rows": [{k: v for k, v in i.items() if k != "resolution"} for i in INCIDENTS]},
    )
    resolutions = [dict(i["resolution"], incident=i["id"]) for i in INCIDENTS if i.get("resolution")]
    g.write(
        "UNWIND $rows AS r MATCH (i:Incident {id: r.incident}), (rb:Runbook {id: r.runbook}) "
        "MERGE (res:Resolution {id: r.id}) SET res.summary = r.summary, res.at = r.at "
        "MERGE (i)-[:RESOLVED_BY]->(res) MERGE (res)-[:FOLLOWED]->(rb)",
        {"rows": resolutions},
    )
    g.write(
        "UNWIND $rows AS r MATCH (res:Resolution {id: r.id}), (d:Deployment {id: r.reverted}) "
        "MERGE (res)-[:REVERTED]->(d)",
        {"rows": [r for r in resolutions if r.get("reverted")]},
    )
    return counts(g)


def counts(g: Graph) -> dict[str, int]:
    rows, _ = g.read("MATCH (n) RETURN labels(n)[0] AS label, count(n) AS c ORDER BY label")
    out = {str(label): int(c) for label, c in rows}
    rows, _ = g.read("MATCH ()-[r]->() RETURN type(r), count(r) ORDER BY type(r)")
    out.update({f"rel:{t_}": int(c) for t_, c in rows})
    return out


def clear_memory(g: Graph) -> int:
    """Delete only agent-written nodes (Investigation, Hypothesis, Action, and Resolutions TraceLens wrote on approval)
    and reopen incidents TraceLens resolved. Seed data is untouched."""
    rows, _ = g.write(
        "MATCH (n) WHERE n:Investigation OR n:Hypothesis OR n:Action OR (n:Resolution AND n.source = 'tracelens') "
        "DETACH DELETE n RETURN count(n)"
    )
    g.write("MATCH (i:Incident) WHERE i.resolved_at IS NOT NULL SET i.status = 'open', i.resolved_at = NULL")
    return int(rows[0][0]) if rows else 0


def reset_and_seed(g: Graph) -> dict[str, int]:
    """Drop the demo graph (only this graph key) and rebuild it deterministically."""
    g.delete()
    return seed(g)


def seed_if_empty(g: Graph) -> bool:
    if g.node_count() == 0:
        seed(g)
        return True
    return False


if __name__ == "__main__":
    import json

    from .db import get_graph

    print(json.dumps(reset_and_seed(get_graph()), indent=2))

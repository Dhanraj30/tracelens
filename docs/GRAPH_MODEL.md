# TraceLens — Graph data model

Graph name: `tracelens` (tests use `tracelens_test`). All timestamps are **integer epoch seconds**.

## Nodes

| Label | Key | Properties | Notes |
|---|---|---|---|
| `Service` | `name` | `tier` (edge/core/worker/datastore/infra/external), `team`, `external` (bool) | 14 in the demo |
| `Deployment` | `id` | `at`, `version`, `author` | 16 in the demo |
| `CodeChange` | `id` | `summary`, `files` (list), `risk` (low/medium/high) | one or more per deployment |
| `ErrorEvent` | `id` | `at`, `message`, `count` | first-seen buckets per service |
| `ErrorSignature` | `key` | — | **shared** node, e.g. `PoolTimeoutError`. This is what makes "similar incidents" a traversal, not text similarity |
| `Incident` | `id` | `title`, `status` (open/resolved), `opened_at`, `symptom` | 3 live + 4 resolved |
| `Resolution` | `id` | `summary`, `at` | |
| `Runbook` | `id` | `title`, `steps` (list) | |
| **Agent-written** | | | |
| `Investigation` | `id` | `at`, `mode`, `outcome`, `tool_calls`, `confidence` | written by `record_investigation` |
| `Hypothesis` | `id` | `status` (supported/rejected/inconclusive), `score`, `kind`, `label`, `confirmed` | rejected ones are kept on purpose |
| `Action` | `id` | `kind`, `status` (proposed/approved/executed), `summary`, `deployment_id`, `runbook_id`, `simulated=true` | |

## Relationships

```
(:Service)-[:DEPENDS_ON {kind: 'sync'|'async', weight: 1|2}]->(:Service)   // A depends on B  ==  A calls B
(:Deployment)-[:DEPLOYED_TO]->(:Service)
(:Deployment)-[:INCLUDES]->(:CodeChange)
(:ErrorEvent)-[:EMITTED_BY]->(:Service)
(:ErrorEvent)-[:HAS_SIGNATURE]->(:ErrorSignature)
(:Incident)-[:AFFECTS]->(:Service)
(:Incident)-[:OBSERVED]->(:ErrorEvent)
(:Incident)-[:RESOLVED_BY]->(:Resolution)
(:Resolution)-[:FOLLOWED]->(:Runbook)
(:Resolution)-[:REVERTED]->(:Deployment)
(:Runbook)-[:APPLIES_TO]->(:Service)

// memory + audit trail, written only by the agent
(:Investigation)-[:INVESTIGATED]->(:Incident)
(:Investigation)-[:CONSIDERED]->(:Hypothesis)
(:Hypothesis)-[:SUSPECTS]->(:Deployment|:CodeChange|:Service)
(:Hypothesis)-[:SUPPORTED_BY {check}]->(:ErrorEvent|:Deployment|:Incident|:Service)
(:Hypothesis)-[:REJECTED_BY  {check}]->(:ErrorEvent|:Deployment|:Incident|:Service)
(:Investigation)-[:PROPOSED]->(:Action)
(:Action)-[:TARGETS]->(:Deployment)
(:Action)-[:FOLLOWS]->(:Runbook)
```

`weight` on `DEPENDS_ON` (sync = 1, async = 2) is the `weightProp` used by `algo.SPpaths`, so the "shortest" path prefers synchronous call chains — the ones that propagate failures immediately.

## Indexes

Range indexes on: `Service.name`, `Incident.id`, `Deployment.id`, `CodeChange.id`, `ErrorEvent.id`, `ErrorSignature.key`, `Runbook.id`, `Investigation.id`, `Hypothesis.id`, `Action.id`.
Created with `CREATE INDEX FOR (n:Label) ON (n.prop)` after checking `CALL db.indexes()` (FalkorDB errors on a duplicate `CREATE INDEX`).

## Demo topology

```
gateway ──► checkout ──► payments-api ──► postgres-payments ◄── fraud-check ◄── payments-api
   │           │  │  │        └──(async)──► kafka ◄──(async)── inventory / notification / refunds-worker
   │           │  │  └──► inventory
   │           │  └──► auth ──► redis-sessions ◄── cart ◄── checkout
   │           └──(async)──► notification
   ├──► cart
   ├──► auth
   └──► search ──► vendor-search-index (external: no deployments, no error events)
refunds-worker ──► payments-api
```

## Scenario timeline (14 Oct 2026, UTC)

| Time | Event | Role |
|---|---|---|
| 13:20 | `dep-cart-1320` → cart | candidate, **ruled out by origin check** (cart never reaches payments-api) |
| 13:52 | `dep-pay-1352` → payments-api, `cc-pool-size` cuts the Postgres pool 50 → 5 | **best-supported root cause** |
| 13:58 | payments-api `PoolTimeoutError` begins | earliest error = origin |
| 14:01 | checkout `UpstreamTimeout` begins | symptom starts **before** the checkout deploy |
| 14:02 | gateway `HTTP502` | downstream |
| 14:05 | `dep-chk-1405` → checkout | the obvious suspect, **ruled out by temporal precedence** |
| 14:08 | refunds-worker `UpstreamTimeout` | Scenario B symptom |
| 14:10 | `inc-1042` opened | Scenario A |
| 14:25 | `inc-1043` opened | Scenario B (memory) |
| 15:12 | search `EmptyResultSet` | Scenario C symptom |
| 15:30 | `inc-1044` opened | Scenario C (inconclusive) |
| −21 days | `inc-0877` PoolTimeoutError on payments-api, rolled back `dep-pay-h1`, runbook `rb-pool-sizing` | history reached via shared `ErrorSignature` |

## Changes from the original brief

- Added `Service.external` and the `external` tier to model the graph boundary (Scenario C).
- Added `Action-[:TARGETS]->Deployment` and `Action-[:FOLLOWS]->Runbook` so the remediation is a traversable link, not a string.
- Added `Hypothesis.confirmed` (set when a human approves the Action) so memory can distinguish "agent-supported" from "operator-confirmed".
- `SUPPORTED_BY` / `REJECTED_BY` may also target `Service` nodes (e.g. the healthy sibling that ruled out a shared dependency).

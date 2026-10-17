# TraceLens — Cypher queries and graph algorithms

> **Generated from `backend/tools/catalog.py` by `scripts/gen_queries_md.py`. Do not edit by hand.**
>
> The agent never writes Cypher. Every query below is person-written, parameterized (`$param`), and
> wrapped in a tool whose arguments are typed and validated. The only values ever inlined are integers
> that passed a range check (FalkorDB does not accept a parameter as a variable-length hop bound).
> Reads run through `GRAPH.RO_QUERY`; only the two `record_*` tools write.

## Entity resolution (runs before any tool query)

Every service / incident name is resolved against the graph first. If it does not resolve, the step
stops and **no tool query runs**.

```cypher
UNWIND $names AS nm MATCH (s:Service {name: nm}) RETURN collect(s.name)
```

```cypher
MATCH (i:Incident {id: $id}) RETURN count(i)
```

## Tool catalog

| # | Tool | Writes | What a result proves |
|---|------|--------|----------------------|
| 1 | `get_incident` | no | Anchors the investigation: the symptom service and the error signatures we must explain. |
| 2 | `upstream_dependencies` | no | Defines the suspect space: only services on this path can be the upstream cause of the symptom. |
| 3 | `deployments_in_window` | no | Every deployment here is a candidate hypothesis. Anything not here cannot be 'the change that broke it'. |
| 4 | `error_timeline` | no | Establishes temporal order: which service failed first, and whether errors predate a deployment. |
| 5 | `healthy_siblings` | no | Common-cause test: if siblings of the suspect that use the same dependency are healthy, the shared dependency is not the cause. |
| 6 | `similar_incidents` | no | History check by graph structure, not text similarity: a past incident with the same signature on the same service, resolved by a rollback, is strong evidence for the same fix. |
| 7 | `path_between` | no | Path check: a candidate can only cause the symptom if the symptom service reaches it through dependencies. Empty result = not connected. |
| 8 | `critical_services` | no | Weights candidates: a change on a high-centrality service has a wider blast radius, so it is examined first and its score is annotated. |
| 9 | `recall` | no | Memory: a root cause the agent already established (and a human may have confirmed) on the same dependency path can be reused instead of re-derived. |
| 10 | `record_investigation` | yes | This is the memory. The next investigation's `recall` reads exactly these nodes — no seed data involved. |
| 11 | `record_action` | yes | Closes the loop: a human-approved action is recorded on the graph and becomes stronger memory for later investigations. |


### 1. `get_incident`

Load an incident, the service it affects and the error events observed on it.

**Proves:** Anchors the investigation: the symptom service and the error signatures we must explain.

**Arguments:** `incident_id` (string)

```cypher
MATCH (i:Incident {id: $id})-[:AFFECTS]->(s:Service)
OPTIONAL MATCH (i)-[:OBSERVED]->(e:ErrorEvent)-[:HAS_SIGNATURE]->(sig:ErrorSignature)
RETURN i.id, i.title, i.status, i.opened_at, i.symptom, s.name, s.tier, s.team,
       collect({id: e.id, at: e.at, message: e.message, count: e.count, signature: sig.key}) AS observed
```

### 2. `upstream_dependencies`

Walk DEPENDS_ON edges from a service to everything it (transitively) relies on, up to `depth` hops.

**Proves:** Defines the suspect space: only services on this path can be the upstream cause of the symptom.

**Arguments:** `service` (string), `depth` (int 1..4)

```cypher
MATCH p = (s:Service {name: $name})-[:DEPENDS_ON*1..{depth}]->(d:Service)
RETURN d.name, d.tier, d.team, d.external, length(p) AS hops,
       [n IN nodes(p) | n.name] AS path,
       [r IN relationships(p) | r.kind] AS kinds
ORDER BY hops, d.name
```

### 3. `deployments_in_window`

Deployments (with their CodeChanges) to any of the given services inside [from, to].

**Proves:** Every deployment here is a candidate hypothesis. Anything not here cannot be 'the change that broke it'.

**Arguments:** `services` (string[]), `from_ts` (epoch seconds), `to_ts` (epoch seconds)

```cypher
UNWIND $services AS nm
MATCH (s:Service {name: nm})<-[:DEPLOYED_TO]-(d:Deployment)
WHERE d.at >= $from AND d.at <= $to
OPTIONAL MATCH (d)-[:INCLUDES]->(c:CodeChange)
RETURN d.id, d.at, d.version, d.author, s.name,
       collect({id: c.id, summary: c.summary, files: c.files, risk: c.risk}) AS changes
ORDER BY d.at
```

### 4. `error_timeline`

First-seen time and volume per (service, error signature) inside [from, to].

**Proves:** Establishes temporal order: which service failed first, and whether errors predate a deployment.

**Arguments:** `services` (string[]), `from_ts` (epoch seconds), `to_ts` (epoch seconds)

```cypher
UNWIND $services AS nm
MATCH (s:Service {name: nm})<-[:EMITTED_BY]-(e:ErrorEvent)-[:HAS_SIGNATURE]->(sig:ErrorSignature)
WHERE e.at >= $from AND e.at <= $to
RETURN s.name, sig.key, min(e.at) AS first_seen, sum(e.count) AS total, collect(e.id) AS event_ids
ORDER BY first_seen
```

### 5. `healthy_siblings`

For each dependency shared with other services, list those sibling dependents and whether they emitted errors in the window.

**Proves:** Common-cause test: if siblings of the suspect that use the same dependency are healthy, the shared dependency is not the cause.

**Arguments:** `service` (string), `from_ts` (epoch seconds), `to_ts` (epoch seconds)

```cypher
MATCH (s:Service {name: $name})-[:DEPENDS_ON]->(dep:Service)<-[:DEPENDS_ON]-(sib:Service)
WHERE sib.name <> $name
OPTIONAL MATCH (sib)<-[:EMITTED_BY]-(e:ErrorEvent)
WHERE e.at >= $from AND e.at <= $to
OPTIONAL MATCH dp = (sib)-[:DEPENDS_ON*1..3]->(s)
RETURN dep.name AS shared_dependency, sib.name AS sibling, count(DISTINCT e) AS error_events,
       collect(DISTINCT e.id) AS event_ids, count(dp) > 0 AS depends_on_candidate
ORDER BY shared_dependency, sibling
```

### 6. `similar_incidents`

Resolved incidents reached through shared ErrorSignature nodes (and flagged if on the same Service), with their Resolution, Runbook and reverted Deployment.

**Proves:** History check by graph structure, not text similarity: a past incident with the same signature on the same service, resolved by a rollback, is strong evidence for the same fix.

**Arguments:** `signature_keys` (string[]), `services` (string[])

```cypher
MATCH (past:Incident {status: 'resolved'})-[:OBSERVED]->(e:ErrorEvent)-[:HAS_SIGNATURE]->(sig:ErrorSignature)
WHERE sig.key IN $signatures
MATCH (past)-[:AFFECTS]->(ps:Service)
OPTIONAL MATCH (past)-[:RESOLVED_BY]->(res:Resolution)
OPTIONAL MATCH (res)-[:FOLLOWED]->(rb:Runbook)
OPTIONAL MATCH (res)-[:REVERTED]->(rd:Deployment)-[:INCLUDES]->(cc:CodeChange)
RETURN past.id, past.title, past.opened_at, ps.name,
       collect(DISTINCT sig.key) AS shared_signatures,
       ps.name IN $services AS same_service,
       res.id, res.summary, rb.id, rb.title, rb.steps, rd.id,
       collect(DISTINCT cc.files) AS reverted_files
ORDER BY past.opened_at DESC
```

### 7. `path_between`

Weighted shortest DEPENDS_ON path from one service to another (algo.SPpaths, sync=1, async=2).

**Proves:** Path check: a candidate can only cause the symptom if the symptom service reaches it through dependencies. Empty result = not connected.

**Arguments:** `from_service` (string), `to_service` (string)

```cypher
MATCH (a:Service {name: $from}), (b:Service {name: $to})
CALL algo.SPpaths({sourceNode: a, targetNode: b, relTypes: ['DEPENDS_ON'], weightProp: 'weight',
                   relDirection: 'outgoing', pathCount: 1, maxLen: 6})
YIELD path, pathWeight
RETURN [n IN nodes(path) | n.name] AS path, [r IN relationships(path) | r.kind] AS kinds, pathWeight
```

### 8. `critical_services`

Load-bearing services by PageRank (how many things depend on you, transitively) and betweenness (how often you sit on dependency paths).

**Proves:** Weights candidates: a change on a high-centrality service has a wider blast radius, so it is examined first and its score is annotated.

**Arguments:** `limit` (int 1..50)

```cypher
CALL algo.pageRank('Service', 'DEPENDS_ON') YIELD node, score
RETURN node.name AS service, score ORDER BY score DESC
```

```cypher
CALL algo.betweenness({nodeLabels: ['Service'], relationshipTypes: ['DEPENDS_ON'], samplingSeed: 7}) YIELD node, score
RETURN node.name AS service, score ORDER BY score DESC
```

### 9. `recall`

Prior investigations whose supported root-cause hypothesis points at a deployment on one of these services inside the window.

**Proves:** Memory: a root cause the agent already established (and a human may have confirmed) on the same dependency path can be reused instead of re-derived.

**Arguments:** `services` (string[]), `from_ts` (epoch seconds), `to_ts` (epoch seconds)

```cypher
MATCH (inv:Investigation)-[:CONSIDERED]->(h:Hypothesis {status: 'supported'})-[:SUSPECTS]->(d:Deployment)-[:DEPLOYED_TO]->(s:Service)
WHERE s.name IN $services AND d.at >= $from AND d.at <= $to
MATCH (inv)-[:INVESTIGATED]->(inc:Incident)
OPTIONAL MATCH (inv)-[:PROPOSED]->(a:Action)
OPTIONAL MATCH (d)-[:INCLUDES]->(cc:CodeChange)
RETURN inv.id, inv.at, inv.outcome, inc.id, inc.title, h.id, h.score, h.confirmed, d.id, d.at, d.version, s.name,
       a.id, a.status, a.kind, a.runbook_id, collect({id: cc.id, summary: cc.summary, risk: cc.risk}) AS changes
ORDER BY inv.at DESC
```

### 10. `record_investigation`

Write back the Investigation, its Hypotheses (supported AND rejected), SUSPECTS edges and SUPPORTED_BY/REJECTED_BY evidence edges, plus the proposed Action.

**Proves:** This is the memory. The next investigation's `recall` reads exactly these nodes — no seed data involved.

**Arguments:** `investigation` (object)

```cypher
MATCH (inc:Incident {id: $incident_id})
MERGE (inv:Investigation {id: $id})
SET inv.at = $at, inv.mode = $mode, inv.outcome = $outcome, inv.tool_calls = $tool_calls, inv.confidence = $confidence
MERGE (inv)-[:INVESTIGATED]->(inc)
```

```cypher
MATCH (inv:Investigation {id: $id})
UNWIND $hyps AS h
MERGE (hy:Hypothesis {id: h.id})
SET hy.status = h.status, hy.score = h.score, hy.kind = h.kind, hy.label = h.label, hy.confirmed = false
MERGE (inv)-[:CONSIDERED]->(hy)
```

```cypher
UNWIND $links AS l
MATCH (hy:Hypothesis {id: l.hypothesis_id})
MATCH (n) WHERE (n:Deployment OR n:CodeChange OR n:Service) AND (n.id = l.target OR n.name = l.target)
MERGE (hy)-[:SUSPECTS]->(n)
```

```cypher
UNWIND $links AS l
MATCH (hy:Hypothesis {id: l.hypothesis_id})
MATCH (n) WHERE (n:ErrorEvent OR n:Deployment OR n:Incident OR n:Service) AND (n.id = l.target OR n.name = l.target)
MERGE (hy)-[r:SUPPORTED_BY {check: l.check}]->(n)
```

```cypher
UNWIND $links AS l
MATCH (hy:Hypothesis {id: l.hypothesis_id})
MATCH (n) WHERE (n:ErrorEvent OR n:Deployment OR n:Incident OR n:Service) AND (n.id = l.target OR n.name = l.target)
MERGE (hy)-[r:REJECTED_BY {check: l.check}]->(n)
```

```cypher
MATCH (inv:Investigation {id: $investigation_id})
MERGE (a:Action {id: $id})
SET a.kind = $kind, a.status = 'proposed', a.summary = $summary, a.deployment_id = $deployment_id, a.runbook_id = $runbook_id, a.simulated = true
MERGE (inv)-[:PROPOSED]->(a)
WITH a
OPTIONAL MATCH (d:Deployment {id: $deployment_id})
FOREACH (_ IN CASE WHEN d IS NULL THEN [] ELSE [1] END | MERGE (a)-[:TARGETS]->(d))
WITH a
OPTIONAL MATCH (rb:Runbook {id: $runbook_id})
FOREACH (_ IN CASE WHEN rb IS NULL THEN [] ELSE [1] END | MERGE (a)-[:FOLLOWS]->(rb))
```

### 11. `record_action`

Update an Action's status (proposed -> approved -> executed). Execution is SIMULATED. Approval marks the supported hypothesis as human-confirmed.

**Proves:** Closes the loop: a human-approved action is recorded on the graph and becomes stronger memory for later investigations.

**Arguments:** `action_id` (string), `status` ('approved' | 'executed' | 'rejected'), `at` (epoch seconds)

```cypher
MATCH (inv:Investigation)-[:PROPOSED]->(a:Action {id: $id})
SET a.status = $status, a.updated_at = $at
WITH inv, a
OPTIONAL MATCH (inv)-[:CONSIDERED]->(h:Hypothesis {status: 'supported'})
SET h.confirmed = CASE WHEN $status IN ['approved', 'executed'] THEN true ELSE h.confirmed END
RETURN a.id, a.status, a.kind, a.summary, a.deployment_id, a.runbook_id, a.simulated, inv.id
```

## Graph algorithms used

| Procedure | Tool | How |
|---|---|---|
| `algo.SPpaths` | `path_between` | weighted shortest path (weightProp `weight`: sync=1, async=2), `relDirection: 'outgoing'`, `maxLen: 6` |
| `algo.pageRank` | `critical_services` | `('Service','DEPENDS_ON')` — which services most things transitively depend on |
| `algo.betweenness` | `critical_services` | `{nodeLabels:['Service'], relationshipTypes:['DEPENDS_ON'], samplingSeed: 7}` — which services sit on the most dependency paths |

Signatures verified against https://docs.falkordb.com/algorithms on FalkorDB v4.22.1 (2026-10-17).

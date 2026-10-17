# The obvious suspect is usually wrong: building an incident agent that argues with itself on FalkorDB

*Draft for the Graph Hacks blog prize. Edit freely — the voice should be yours.*

At 14:10 checkout starts failing. At 14:05 someone deployed to checkout. Case closed?

That's the trap every on-call engineer knows. In our hackathon project, **TraceLens**, we wanted an agent that refuses to fall into it — one that treats "the last deploy" as a *hypothesis*, tries to falsify it with evidence, and shows you the suspects it ruled out alongside the one it kept.

We built it in a weekend for Graph Hacks: Context for AI Agents, on FalkorDB. Here's what we learned.

## Start from the symptom, walk upstream

The organizers' reference demo (Blast Radius) answers "X is down — what else breaks?" That's a downstream walk. We wanted the opposite direction: "X is failing — what upstream change explains it?"

In a graph, that's one query:

```cypher
MATCH p = (s:Service {name: $name})-[:DEPENDS_ON*1..4]->(d:Service)
RETURN d.name, [n IN nodes(p) | n.name] AS path, length(p) AS hops
```

Everything checkout transitively relies on *is* the suspect space. Nothing outside it can be the cause. That single traversal turns "look at everything" into "look at these nine services".

## Hypotheses you can falsify

For each deployment on that path in the last hour, the agent creates a hypothesis and runs five graph checks:

1. **Temporal precedence.** Did the symptom start *before* the deploy? Then the deploy can't be the cause. Checkout's `UpstreamTimeout` errors began at 14:01; the checkout deploy was 14:05. Ruled out — with the two node IDs that prove it.
2. **Dependency path.** `algo.SPpaths` from the symptom service to the candidate, weighted so synchronous calls cost less than async ones. No path, no causation.
3. **Error origin.** The earliest error in the window sits at `payments-api` (13:58). A candidate must sit at or above that point. The cart deploy at 13:20 passes the time check but cart never reaches payments-api. Ruled out.
4. **Common cause.** payments-api and fraud-check share the same Postgres. fraud-check is healthy. So Postgres is not the problem — the fault is local to payments-api. (Services that *depend on* the candidate are excluded here; their errors are expected.)
5. **History.** Because `ErrorSignature` is a shared node, "have we seen `PoolTimeoutError` on payments-api before?" is a two-hop traversal, not a fuzzy text search. It found an incident from three weeks ago, resolved by a rollback that touched the same file.

The survivor: a payments-api deploy at 13:52 that cut the DB connection pool from 50 to 5. Five of five checks. We call that the *best-supported explanation* — never "proven". Confidence is the share of checks passed, and the UI says so.

## The agent never writes Cypher

This was our most important design rule. Every query is person-written, parameterized, and wrapped in a tool with typed arguments. Names are regex-validated and then resolved against the graph; if a name doesn't resolve, the step stops and no query runs. Reads go through `GRAPH.RO_QUERY`, which FalkorDB enforces as read-only.

An LLM, if you turn it on, can only write the final narrative — and a validator rejects any narrative that names an entity not present in the evidence. Everything else is a deterministic state machine in one file, capped at 14 tool calls, with every call traced: tool, arguments, exact Cypher, parameters, milliseconds, rows. The trace is the product.

## Memory is just more graph

When the investigation finishes, the agent writes it back:

```
(:Investigation)-[:CONSIDERED]->(:Hypothesis {status:'rejected'})-[:REJECTED_BY {check:'temporal_precedence'}]->(:ErrorEvent)
```

Yes, the rejected ones too. When refunds start failing twenty minutes later, the first thing the agent does is `recall`: a traversal through `Investigation → Hypothesis(supported) → SUSPECTS → Deployment → DEPLOYED_TO → Service` restricted to the new incident's dependency path. It finds the payments-api deploy, re-checks the timeline for refunds-worker, and answers in five tool calls instead of eleven — with "learned 17 Oct, operator confirmed". Reset the memory and it goes back to the long way. No seed data involved.

## Saying "I don't know" is a feature

The third scenario has no deployments in the window and a dependency path that leaves the modeled graph at an external search vendor. The right answer is *inconclusive*, with a list of exactly what was checked and which missing data would resolve it. An agent that always finds a root cause is an agent you can't trust.

## Things we learned about FalkorDB

- You can't pass a parameter as a variable-length hop bound (`*1..$depth`). Validate the integer and inline it.
- `CREATE INDEX` on an already-indexed property errors; check `CALL db.indexes()` first.
- `algo.SPpaths` with a `weightProp` is a great fit for "is this candidate reachable, and how directly?" — put a `weight` on your edges from day one.
- `GRAPH.RO_QUERY` rejecting writes is a free safety net for a tool catalog.

## What we'd do next

Real ingestion (deploy webhooks, error buckets), letting the LLM choose the *order* of tools from the catalog, and a second hop of memory: "the last three times this signature appeared, the fix was X".

Code, queries, graph model and a 2-minute video: _repo link_.

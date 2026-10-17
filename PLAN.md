# TraceLens — Plan (milestones only)

Event: Graph Hacks: Context for AI Agents (WeMakeDevs × FalkorDB). Deadline Sun 18 Oct 2026 23:59 IST; internal deadline 21:00; feature freeze 16:00.
Tracks: 01 Agents That Act on Connected Data (primary); 02 Agent Memory and Coordination (if Scenario B works).

| When (IST) | Milestone | Done means |
|---|---|---|
| Sat 17:00 | Env + FalkorDB up + PLAN | FalkorDB answers a query ✅ (16:20) |
| Sat 21:00 | P0 schema, seed, tool catalog | every tool runs on real FalkorDB; pytest green |
| Sun 01:00 | P1 investigation engine + API | Scenarios A/B/C hit ground truth via API |
| Sun 13:00 | P2 dashboard on real API | Scenario A full flow in browser |
| Sun 16:00 | P3 memory polish, MCP, deploy (needs approval) | clean-setup run passes |
| Sun 16:00 | Feature freeze | fixes only |
| Sun 19:00 | review, QA, docs, demo script, video | |
| Sun 21:00 | submission ready | |

Cut order if behind: MCP → LLM mode → live deploy → graph animation.
Never cut: real queries, Scenario A with rejected suspect, trace panel, README, video.

Decisions log
- gstack skipped: Bun not installed; installing it changes global PATH (needs approval).
- FalkorDB image pinned by digest in docker-compose.yml (falkordb/falkordb@sha256:b0bdcac2…).
- Verified on FalkorDB v4.22.1: param hop bounds unsupported → validate int + inline; CREATE INDEX twice errors → check db.indexes() first; algo.SPpaths/pageRank/betweenness signatures confirmed; ro_query rejects writes.

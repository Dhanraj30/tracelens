# Submission checklist — Graph Hacks: Context for AI Agents

Deadline: **Sunday 18 Oct 2026, 11:59 PM IST**. Internal: submit by 21:00 IST.

| Rule | Status | Where |
|---|---|---|
| Public repository | ☐ user: push to GitHub + make public | `git remote add origin … && git push -u origin main` |
| README with setup | ☑ | `README.md` → "30-second quick start" |
| README with graph data model | ☑ | `README.md` → "Graph data model", full detail in `docs/GRAPH_MODEL.md` |
| Cypher queries / algorithms we depend on | ☑ | `docs/QUERIES.md` (generated from the tool catalog) |
| Demo video (2–3 min) | ☐ user: record using `docs/DEMO_SCRIPT.md` | link goes in README → "Demo video" |
| Live deployment **or** complete local setup | ☑ local: `docker compose up --build` · ☐ live: needs user approval | README → "Run it" |
| Track names | ☑ Track 01 (primary), Track 02 (memory) | README → "Tracks" |
| Disclosure of AI coding assistant use | ☑ | README → "AI-assistance disclosure" |
| FalkorDB does real work | ☑ 11 tools, 3 algorithms, memory as graph | `docs/EXPLAIN_IT.md` table |
| Main coding work new during the event | ☑ first commit Sat 17 Oct 2026 16:xx IST | `git log` |
| Participant can explain model/queries/architecture | ☐ user: read `docs/EXPLAIN_IT.md` and tick its checklist | |
| Tests against a real FalkorDB | ☑ `pytest` (27 tests) | `tests/` |
| No secrets committed | ☑ `.env` ignored, `.env.example` only | `.gitignore` |
| Blog post (side prize, optional) | ☐ user: edit + publish `docs/BLOG_DRAFT.md` | |

## Before you submit

1. `docker compose down -v && docker compose up --build` from a clean clone → open http://localhost:8000 → run Scenario A.
2. Paste the video link and the repo link into the README.
3. Double-check `git log` shows no `.env` and no keys.
4. Fill the submission form: project name, repo URL, video URL, tracks (01 + 02), AI-assistance disclosure text (copy from README).

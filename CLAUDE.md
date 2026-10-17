# TraceLens — project notes for Claude Code

Hackathon project (Graph Hacks: Context for AI Agents, WeMakeDevs × FalkorDB). Full brief: `TRACELENS_PROMPT (1).md`. Plan: `PLAN.md`.

## Non-negotiables
- FalkorDB must do real work: every conclusion comes from a catalog tool (graph query / algorithm). Never hardcode root causes; ground truth lives only in `tests/scenarios.yaml`.
- The agent never writes Cypher. Add queries only in `backend/tools/catalog.py`, then regenerate docs: `python -m scripts.gen_queries_md`.
- Never publish / deploy / push to a public remote / use a paid service / change global config without explicit approval from the user.
- Language: "best-supported explanation", never "proven". Confidence = share of checks passed.

## Run
```
docker compose up -d falkordb                      # FalkorDB on 6379, Browser on 3000
.venv/Scripts/python -m backend.seed              # reset + seed demo graph
.venv/Scripts/python -m uvicorn backend.api.main:app --reload --port 8000
cd frontend && npm run dev                        # Vite on 5173 (proxies /api -> 8000)
PYTHONIOENCODING=utf-8 .venv/Scripts/python -m pytest -q   # uses graph key `tracelens_test`
.venv/Scripts/python -m backend.mcp_client_demo inc-1042   # MCP: client picks catalog tools over stdio
docker compose up --build                                  # full image: FastAPI serves built frontend on 8000
```
Headless screenshots for QA: `chrome --headless=new --window-size=1920,1080 --virtual-time-budget=12000 --screenshot=out.png "http://127.0.0.1:5173/?incident=inc-1042&instant=1"` (`&step=N` pauses mid-replay, `&tab=hypotheses`).

## Layout
- `backend/db.py` traced FalkorDB access (`read` = RO_QUERY, `write`), `QueryRecord` is what the UI shows under "Show Cypher".
- `backend/seed.py` deterministic seed (fixed IDs/timestamps). `backend/tools/` catalog + validation. `backend/agent/engine.py` bounded state machine. `backend/api/main.py` FastAPI.
- `frontend/` Vite + React + TS + Tailwind + @xyflow/react + dagre.

## FalkorDB gotchas (verified on v4.22.1)
- Parameters are NOT accepted as variable-length hop bounds → validate int, inline.
- `CREATE INDEX` twice errors → check `CALL db.indexes()` first (`Graph.ensure_index`).
- `GRAPH.RO_QUERY` rejects writes (good: safety net for the read tools).
- `algo.SPpaths({sourceNode, targetNode, relTypes, weightProp, relDirection, pathCount, maxLen}) YIELD path, pathWeight`.
- `algo.pageRank(label, relType) YIELD node, score`; `algo.betweenness({nodeLabels, relationshipTypes, samplingSeed}) YIELD node, score`.

## gstack
Not installed: Bun is not present on this machine and installing it changes global PATH (needs user approval).

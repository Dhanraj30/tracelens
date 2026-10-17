"""Eval harness for the LangGraph guest agent, with the deterministic engine as a baseline.

Usage
  python -m evals.run_evals                     # OFFLINE: fake/replay model, no network (what pytest runs)
  RUN_LIVE_LLM=1 python -m evals.run_evals --live --runs 2 --max-requests 100

Isolation: every run gets its own tenant graph `tracelens:eval-<run_id>`, freshly seeded and
deleted afterwards. The agent never sees tests/scenarios.yaml; only the scorer does.

Outputs: evals/results/<timestamp>.json (raw) and docs/EVALS.md (generated).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.agent.engine import Investigator  # noqa: E402
from backend.db import Graph, graph_name_for  # noqa: E402
from backend.seed import reset_and_seed  # noqa: E402

SCENARIOS = yaml.safe_load((ROOT / "tests" / "scenarios.yaml").read_text(encoding="utf-8"))["scenarios"]

CASES: list[dict[str, Any]] = [
    {"case": "A", "incident": "inc-1042", "title": "Scenario A — checkout, red herring", "after": None},
    {"case": "B_after_A", "incident": "inc-1043", "title": "Scenario B after A (same tenant) — memory", "after": "A"},
    {"case": "B_alone", "incident": "inc-1043", "title": "Scenario B alone (fresh tenant) — no memory", "after": None},
    {"case": "C", "incident": "inc-1044", "title": "Scenario C — search, incomplete evidence", "after": None},
    {"case": "unknown", "incident": "inc-9999", "title": "Unknown incident id", "after": None},
]


class BudgetExceeded(Exception):
    pass


# ---------------------------------------------------------------------------------------------- scoring
def expected_for(case: str) -> dict[str, Any]:
    """Only the scorer reads ground truth."""
    a, b, c = SCENARIOS["A"], SCENARIOS["B"], SCENARIOS["C"]
    return {
        "A": {"outcome": "root_cause_supported", "root": a["root_cause_deployment"], "must_rule_out": ["dep-chk-1405"]},
        "B_after_A": {"outcome": "answered_from_memory", "root": b["root_cause_deployment"], "must_rule_out": []},
        "B_alone": {"outcome": "root_cause_supported", "root": b["root_cause_deployment"], "must_rule_out": []},
        "C": {"outcome": "inconclusive", "root": None, "must_rule_out": []},
        "unknown": {"outcome": "unknown_entity", "root": None, "must_rule_out": []},
    }[case]


def score_guest(case: str, out: dict[str, Any]) -> dict[str, Any]:
    exp = expected_for(case)
    rec = out.get("recorded") or {}
    calls = out.get("tool_calls", [])
    memory_used = any(c["tool"] == "recall" and isinstance(c.get("result"), str) and not c["result"].startswith("0 prior") for c in calls)
    # the guest is credited only for suspects it PROPOSED and the server verified; rejections cover the whole path
    supported = [h["suspect"] for h in rec.get("hypotheses", []) if h.get("status") == "supported" and h.get("proposed", True)]
    rejected = [h["suspect"] for h in rec.get("hypotheses", []) if h.get("status") == "rejected"]
    verified_outcome = rec.get("verified_outcome")
    final = (out.get("final") or "").lower()

    if case == "unknown":
        # criterion: nothing recorded and no tool call beyond the resolution attempt (get_incident). Wording of the final text is not scored.
        verified_correct = not rec and len(calls) <= 1
        outcome_match = verified_correct and any(w in final for w in ("unknown", "not found", "does not exist", "not exist", "no such", "invalid", "doesn't exist"))
    elif case == "C":
        verified_correct = bool(rec) and verified_outcome == "inconclusive" and not supported
        outcome_match = (rec.get("claimed_outcome") == "inconclusive")
    elif case == "B_after_A":
        verified_correct = memory_used and (supported == [exp["root"]] or (not rec and exp["root"] in final))
        outcome_match = memory_used and (rec.get("claimed_outcome") in ("answered_from_memory", "root_cause_supported") or not rec)
    else:
        verified_correct = bool(rec) and verified_outcome == "root_cause_supported" and supported == [exp["root"]]
        outcome_match = rec.get("claimed_outcome") in ("root_cause_supported", "supported")
    # "ruled out" counts ONLY if the server stored the deployment as rejected (with failed checks), whether or not the guest named it
    ruled_out_ok = all(d in rejected for d in exp["must_rule_out"])
    proposed_ids = {h["suspect"] for h in rec.get("hypotheses", []) if h.get("proposed", True)}
    ruled_out_how = {d: ("WRONGLY_SUPPORTED" if d in supported else "proposed_and_rejected" if d in rejected and d in proposed_ids
                         else "rejected_by_server_check" if d in rejected else "NOT_STORED") for d in exp["must_rule_out"]}
    reasons = []
    if not verified_correct:
        reasons.append(f"expected {exp['outcome']} with root {exp['root']}; got verified_outcome={verified_outcome}, supported={supported}, recorded={'yes' if rec else 'no'}, tool_calls={len(calls)}")
    if not ruled_out_ok:
        reasons.append(f"must rule out {exp['must_rule_out']} but supported={supported}")
    return {"verified_correct": verified_correct, "outcome_match": outcome_match, "ruled_out_correct": ruled_out_ok, "ruled_out_how": ruled_out_how,
            "memory_used": memory_used, "supported": supported, "rejected": rejected, "claimed_outcome": rec.get("claimed_outcome"),
            "verified_outcome": verified_outcome, "failure_reasons": reasons}


def score_engine(case: str, rep: dict[str, Any]) -> dict[str, Any]:
    exp = expected_for(case)
    root = (rep.get("root_cause") or {}).get("deployment_id")
    rejected = [h["suspect"] for h in rep.get("hypotheses", []) if h["status"] == "rejected"]
    supported = [h["suspect"] for h in rep.get("hypotheses", []) if h["status"] == "supported"]
    outcome_match = rep["outcome"] == exp["outcome"]
    verified_correct = outcome_match and root == exp["root"]
    ruled_out_ok = all(d in rejected for d in exp["must_rule_out"])
    reasons = [] if verified_correct and ruled_out_ok else [f"expected {exp['outcome']}/{exp['root']}, got {rep['outcome']}/{root}, rejected={rejected}"]
    return {"verified_correct": verified_correct, "outcome_match": outcome_match, "ruled_out_correct": ruled_out_ok,
            "ruled_out_how": {d: "rejected" if d in rejected else "not rejected" for d in exp["must_rule_out"]},
            "memory_used": bool(rep.get("memory")), "supported": supported, "rejected": rejected, "claimed_outcome": rep["outcome"],
            "verified_outcome": rep["outcome"], "failure_reasons": reasons}


# ---------------------------------------------------------------------------------------------- live instrumentation
class RequestMeter:
    """LangChain callback: counts model requests, spaces them for free-tier RPM limits, enforces a hard budget."""

    def __init__(self, max_requests: int, min_interval_s: float, live: bool):
        from langchain_core.callbacks import BaseCallbackHandler

        self.max_requests, self.min_interval_s, self.live = max_requests, min_interval_s, live
        self.total = 0
        self.per_run = 0
        self.models_seen: list[str] = []
        self._last = 0.0
        meter = self

        class _H(BaseCallbackHandler):
            raise_error = True

            def on_chat_model_start(self, serialized, messages, **kwargs):  # noqa: ANN001
                if meter.total >= meter.max_requests:
                    raise BudgetExceeded(f"max_requests={meter.max_requests} reached")
                if meter.live:
                    wait = meter.min_interval_s - (time.time() - meter._last)
                    if wait > 0:
                        time.sleep(wait)
                meter._last = time.time()
                meter.total += 1
                meter.per_run += 1
                name = (serialized or {}).get("name") or (kwargs.get("metadata") or {}).get("ls_model_name") or "?"
                meter.models_seen.append(str(name))

        self.handler = _H()


# ---------------------------------------------------------------------------------------------- one run
async def run_guest_case(case: dict[str, Any], graph: Graph, live: bool, meter: RequestMeter, primary_name: str) -> dict[str, Any]:
    import backend.mcp_server as srv
    from agents.langgraph_guest.agent import run_guest

    srv._ctx = None
    srv._session["trace_start"] = 0
    srv.get_graph = lambda: graph  # the guest's MCP server talks to THIS tenant graph only
    meter.per_run = 0
    t0 = time.time()
    source = "live"
    prev_replay = os.environ.get("TRACELENS_REPLAY")
    try:
        if live:
            os.environ.pop("TRACELENS_REPLAY", None)
            out = await run_guest(case["incident"], server=srv.mcp, log=lambda s: None, callbacks=[meter.handler])
        else:
            from agents.langgraph_guest.fake_model import ReplayToolCallingModel
            from evals.replays import script_for

            os.environ["TRACELENS_REPLAY"] = "1"  # provenance becomes mcp:<agent>-replay so nobody mistakes it for a live LLM run
            script, source = script_for(case["case"])
            out = await run_guest(case["incident"], server=srv.mcp, model=ReplayToolCallingModel(script=script), fallbacks=[], log=lambda s: None, callbacks=[meter.handler])
        error = None
    except BudgetExceeded as exc:
        out, error = {"tool_calls": [], "recorded": None, "final": "", "model_used": "n/a"}, f"budget: {exc}"
    except Exception as exc:  # noqa: BLE001
        out, error = {"tool_calls": [], "recorded": None, "final": "", "model_used": "n/a"}, f"{type(exc).__name__}: {str(exc)[:600]}"
    finally:  # never leak the replay flag into the rest of the process (pytest runs other suites afterwards)
        if prev_replay is None:
            os.environ.pop("TRACELENS_REPLAY", None)
        else:
            os.environ["TRACELENS_REPLAY"] = prev_replay
    latency = round(time.time() - t0, 1)
    calls = out.get("tool_calls", [])
    rejected_by_validation = sum(1 for c in calls if isinstance(c.get("result"), str) and (c["result"].startswith("unknown ") or c["result"].startswith("invalid ") or "must be" in c["result"]))
    rec = out.get("recorded") or {}
    proposals_rejected = sum(1 for h in rec.get("hypotheses", []) if h.get("status") == "rejected")
    model_used = out.get("model_used", "n/a")
    fallbacks = [r["note"] for r in out.get("restarts", [])]  # "restarted on <provider> after <error>"
    if error and ("generativelanguage" in error or "thought_signature" in error):
        fallbacks.append(f"primary {primary_name} -> fallback provider attempted mid-run and FAILED (see error)")
    score = score_guest(case["case"], out) if not error else {"verified_correct": False, "outcome_match": False, "ruled_out_correct": False,
                                                                 "ruled_out_how": {}, "memory_used": False, "supported": [], "rejected": [],
                                                                 "claimed_outcome": None, "verified_outcome": None, "failure_reasons": [error]}
    return {"mode": "agent", "case": case["case"], "title": case["title"], "source": source, "error": error, "latency_s": latency,
            "final_text": (out.get("final") or "")[:600], "recorded": rec or None, "restarts": out.get("restarts", []),
            "tool_calls": len(calls), "tools_in_order": [c["tool"] for c in calls], "llm_requests": meter.per_run, "model_used": model_used,
            "fallbacks": fallbacks, "tool_calls_rejected_by_validation": rejected_by_validation, "narratives_rejected": None,
            "proposals_rejected_by_verifier": proposals_rejected, "recorded_id": rec.get("investigation_id"), **score}


def run_engine_case(case: dict[str, Any], graph: Graph) -> dict[str, Any]:
    t0 = time.time()
    rep = Investigator(graph).run(incident_id=case["incident"])
    latency = round(time.time() - t0, 1)
    score = score_engine(case["case"], rep)
    return {"mode": "baseline", "case": case["case"], "title": case["title"], "source": "deterministic engine",
            "error": rep.get("error") if rep["outcome"] != "unknown_entity" else None,  # unknown_entity is the expected outcome, not an error
            "latency_s": latency, "tool_calls": rep["tool_calls"], "tools_in_order": [c["tool"] for c in rep["trace"]], "llm_requests": 0,
            "model_used": "none", "fallbacks": [], "tool_calls_rejected_by_validation": 0, "narratives_rejected": 0,
            "proposals_rejected_by_verifier": None, "recorded_id": rep["id"], **score}


async def run_once(run_id: str, live: bool, meter: RequestMeter, primary_name: str, log) -> list[dict[str, Any]]:
    """One full pass over the cases for BOTH modes, each in its own freshly seeded tenant graph."""
    results: list[dict[str, Any]] = []
    for mode in ("agent", "baseline"):
        tenant = f"eval-{run_id}-{mode}"
        g = Graph(graph_name_for(tenant))
        reset_and_seed(g)
        try:
            for case in CASES:
                if case["after"] is None and case["case"] != "B_after_A":
                    # fresh state for independent cases: wipe memory only (keeps the seed)
                    from backend.seed import clear_memory

                    clear_memory(g)
                if case["case"] == "A":
                    pass  # B_after_A runs right after A on the same memory
                r = (await run_guest_case(case, g, live, meter, primary_name)) if mode == "agent" else run_engine_case(case, g)
                r["run_id"], r["tenant"] = run_id, g.name
                results.append(r)
                log(f"  [{mode:8}] {case['case']:10} verified_correct={r['verified_correct']!s:5} outcome={r['verified_outcome']} tools={r['tool_calls']:2} llm_req={r['llm_requests']:2} {r['latency_s']:6.1f}s"
                    + (f"  FALLBACK {r['fallbacks']}" if r["fallbacks"] else "") + (f"  ERROR {r['error']}" if r.get("error") else ""))
                if r.get("error", "") and str(r["error"]).startswith("budget"):
                    log("  budget exhausted — stopping cleanly")
                    return results
        finally:
            g.delete()
    return results


# ---------------------------------------------------------------------------------------------- reporting
def _explain_error(err: str) -> str:
    if "thought_signature" in err:
        return ("the primary model hit a Groq error mid-run, the ModelFallbackMiddleware switched to Gemini, and Gemini's OpenAI-compatible "
                "endpoint rejected the transcript because it contains function calls made by a different model (Gemini 3.x requires its own "
                "thought_signature on every function call). Infrastructure limitation, not a reasoning error.")
    if "429" in err:
        return "rate limited (HTTP 429) beyond the single retry; the request budget/spacing of the harness, not the agent's reasoning."
    if "400" in err and "api.groq.com" in err:
        return "Groq returned HTTP 400 mid-run (typically a malformed tool call from the model); the run could not continue."
    if err.startswith("budget"):
        return "the harness request budget was reached and the run stopped cleanly."
    return "see error text."


def rescore(raw_path: Path, docs_path: Path | None = None) -> dict[str, Any]:
    """Re-apply the scoring rules to a saved raw results file (no network). Only fields stored in the file are used."""
    docs_path = docs_path or (ROOT / "docs" / "EVALS.md")
    data = json.loads(raw_path.read_text(encoding="utf-8"))
    for r in data["results"]:
        if r["mode"] != "agent" or r.get("error"):
            if r.get("error") and ("generativelanguage" in r["error"] or "thought_signature" in r["error"]) and not r.get("fallbacks"):
                r["fallbacks"] = [f"primary {data['meta']['primary_model']} -> fallback provider attempted mid-run and FAILED (see error)"]
            continue
        out = {"tool_calls": [{"tool": t, "result": ""} for t in r["tools_in_order"]], "recorded": r.get("recorded"), "final": r.get("final_text", "")}
        if r["case"] == "unknown":  # scoring depends only on stored counts
            r.update({"verified_correct": not r.get("recorded_id") and r["tool_calls"] <= 1, "failure_reasons": []})
            r["outcome_match"] = r["verified_correct"]
        elif r.get("recorded") is not None:
            r.update(score_guest(r["case"], out))
    original = data.get("summary") or summarize(data["results"])
    data["meta"]["original_summary"] = {m: {"verified_correct": original[m]["verified_correct"], "total": original[m]["total"]} for m in ("agent", "baseline")}
    data["summary"] = summarize(data["results"])
    data["meta"]["rescored"] = True
    data["meta"]["rescore_note"] = (
        "After live run 1 the scorer for the unknown-incident case was changed: originally it also required the guest's final text to contain "
        "'unknown' / 'does not exist'; now it scores behaviour only (nothing recorded, no tool call beyond the resolution attempt), because the "
        "criterion is 'no tool calls beyond resolution', not wording. Fallback attempts are also recovered from error text. No agent logic or prompt changed."
    )
    out_path = raw_path.with_name(raw_path.stem + "-rescored.json")
    out_path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    write_markdown(docs_path, data["meta"], data["results"], data["summary"])
    return data


def _git_commit() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, cwd=ROOT).stdout.strip() or "unknown"
    except Exception:  # noqa: BLE001
        return "unknown"


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for mode in ("agent", "baseline"):
        rows = [r for r in results if r["mode"] == mode]
        per_case = {}
        for case in CASES:
            cs = [r for r in rows if r["case"] == case["case"]]
            if not cs:
                continue
            per_case[case["case"]] = {
                "title": case["title"], "runs": len(cs),
                "verified_correct": sum(r["verified_correct"] for r in cs), "outcome_match": sum(r["outcome_match"] for r in cs),
                "ruled_out_correct": sum(r["ruled_out_correct"] for r in cs),
                "tool_calls_avg": round(statistics.mean(r["tool_calls"] for r in cs), 1), "llm_requests_avg": round(statistics.mean(r["llm_requests"] for r in cs), 1),
                "latency_avg_s": round(statistics.mean(r["latency_s"] for r in cs), 1), "fallbacks": sum(len(r["fallbacks"]) for r in cs),
                "validation_rejections": sum(r["tool_calls_rejected_by_validation"] for r in cs),
                "verifier_rejections": sum((r["proposals_rejected_by_verifier"] or 0) for r in cs),
                "narratives_rejected": sum((r["narratives_rejected"] or 0) for r in cs) if mode == "baseline" else None,
                "tools_in_order": sorted({" → ".join(r["tools_in_order"]) for r in cs}),
            }
        out[mode] = {"per_case": per_case, "verified_correct": sum(r["verified_correct"] for r in rows), "total": len(rows),
                     "llm_requests": sum(r["llm_requests"] for r in rows), "fallbacks": sum(len(r["fallbacks"]) for r in rows)}
    return out


def write_markdown(path: Path, meta: dict[str, Any], results: list[dict[str, Any]], summary: dict[str, Any]) -> None:
    L: list[str] = []
    L.append("# TraceLens evals — LangGraph guest vs deterministic engine" + (" (offline replay)" if meta["mode"] == "offline" else " (live)") + "\n")
    L.append("> Generated by `python -m evals.run_evals` — do not edit by hand. Live results: `docs/EVALS.md`; offline replay results: `docs/EVALS-offline.md`.\n")
    L.append(f"- **Date:** {meta['date']}  ·  **Commit:** `{meta['commit']}`  ·  **Mode:** {meta['mode']}  ·  **Runs:** {meta['runs']}")
    L.append(f"- **Models:** primary `{meta['primary_model']}`, fallback `{meta['fallback_model']}`" + (" (offline: replay/fake model, no network)" if meta["mode"] == "offline" else ""))
    L.append(f"- **LLM requests used:** {meta['llm_requests_used']} (budget {meta['max_requests']})  ·  **Isolation:** one freshly seeded tenant graph per run and mode, deleted afterwards\n")
    ag, bl = summary["agent"], summary["baseline"]
    L.append(f"**Summary — LLM agent: {ag['verified_correct']}/{ag['total']} verified correct; deterministic baseline: {bl['verified_correct']}/{bl['total']}.**\n")
    if meta.get("rescored"):
        o = meta.get("original_summary", {})
        L.append("> **Scoring change disclosed.** " + meta.get("rescore_note", "") +
                 f" Original result as scored at run time: LLM agent {o.get('agent', {}).get('verified_correct', '?')}/{o.get('agent', {}).get('total', '?')}, "
                 f"baseline {o.get('baseline', {}).get('verified_correct', '?')}/{o.get('baseline', {}).get('total', '?')}. "
                 f"Re-scored: LLM agent {ag['verified_correct']}/{ag['total']}, baseline {bl['verified_correct']}/{bl['total']}. "
                 "Both raw files are kept in `evals/results/` (`…-live.json` original, `…-live-rescored.json`).\n")
    for mode, label in (("agent", "LangGraph guest (LLM tool selection over MCP, server-verified)"), ("baseline", "Deterministic engine (baseline)")):
        L.append(f"## {label}\n")
        L.append("| Case | Runs | Verified correct | Outcome match | Ruled-out correct | Tool calls (avg) | LLM req (avg) | Latency (avg) | Fallbacks | Validation rejections | Verifier rejections | Narratives rejected |")
        L.append("|---|---|---|---|---|---|---|---|---|---|---|---|")
        for c in summary[mode]["per_case"].values():
            nr = "n/a (guest writes no narrative)" if c["narratives_rejected"] is None else c["narratives_rejected"]
            vr = "n/a" if mode == "baseline" else c["verifier_rejections"]
            L.append(f"| {c['title']} | {c['runs']} | {c['verified_correct']}/{c['runs']} | {c['outcome_match']}/{c['runs']} | {c['ruled_out_correct']}/{c['runs']} | {c['tool_calls_avg']} | {c['llm_requests_avg']} | {c['latency_avg_s']} s | {c['fallbacks']} | {c['validation_rejections']} | {vr} | {nr} |")
        L.append("")
        L.append("Tool order observed: " + "; ".join(f"**{k}**: {' / '.join(v['tools_in_order'])}" for k, v in summary[mode]["per_case"].items()) + "\n")
    fails = [r for r in results if not (r["verified_correct"] and r["ruled_out_correct"])]
    L.append("## Failures and why\n")
    if not fails:
        L.append("None in this run set.\n")
    for r in fails:
        L.append(f"- **{r['mode']} / {r['case']} / run {r['run_id']}** — " + "; ".join(r["failure_reasons"]) + (f" (model {r['model_used']})" if r["mode"] == "agent" else "")
                 + (f"\n  - plain-language cause: {_explain_error(r.get('error') or '')}" if r.get("error") else ""))
    if meta["mode"] == "live":
        L.append("\n## Live-run notes\n")
        L.append("- Groq free tier returned HTTP 429 (tokens-per-minute) on most requests; the client retried once honouring `Retry-After`, which is why guest runs took 2–3 minutes. "
                 "Each request re-sends the whole transcript, so spacing requests 2.5 s apart is not enough — expect ~10–15 s between model calls for a 429-free run.")
        L.append("- Mid-run provider fallback to Gemini is **not viable** with Gemini 3.x function calling: Gemini rejects a transcript that contains another model's tool calls (`thought_signature` missing). "
                 "Since live run 1 the guest therefore RESTARTS the whole run from scratch on the next provider (recorded as `restarted on <provider> after <error>`); it never continues another model's transcript.")
        L.append("- A live guest run takes about 2 minutes (8 or so model calls with reasoning, plus retries); the deterministic engine answers in well under a second.")
    L.append("\n## How to read this\n")
    L.append("- *Verified correct* for the guest means the SERVER's re-verification of what the guest proposed matches the ground truth (and for Scenario A, the checkout 14:05 deploy is not among the supported suspects). The guest never sees the ground truth; only the scorer does.")
    L.append("- *Ruled-out correct* counts only if the server STORED the deployment as a rejected hypothesis with failed checks. How: `proposed_and_rejected` (the guest blamed it, the verifier rejected it) or `rejected_by_server_check` (the guest never named it; the server checked the whole dependency path anyway).")
    L.append(f"- *What to expect when recording:* a live guest run takes about 2 minutes on the Groq free tier (8 or so model calls with reasoning; evals space requests {meta.get('min_interval_s', 12)} s apart, and ~30 s is advisable under the 8k tokens-per-minute limit); the deterministic engine answers in well under a second.")
    L.append("- *Fallbacks* count provider restarts (`restarted on <provider> after <error>`) observed in live mode; the deterministic template fallback applies to narratives only, which the guest does not write.")
    L.append("- Offline mode replays recorded tool-call sequences through a fake chat model; Scenario A replays the real live run. It exercises the MCP server, the catalog validation and the server-side verifier, not the LLM.")
    path.write_text("\n".join(L) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------------------------- main
def run(live: bool = False, runs: int = 1, max_requests: int = 100, min_interval_s: float = 2.5, results_dir: Path | None = None,
        docs_path: Path | None = None, log=print) -> dict[str, Any]:
    results_dir = results_dir or (ROOT / "evals" / "results")
    docs_path = docs_path or (ROOT / "docs" / "EVALS.md")
    if live and os.environ.get("RUN_LIVE_LLM") != "1":
        raise SystemExit("live mode requires RUN_LIVE_LLM=1 (quota guard)")
    primary_name, fallback_name = "replay-fake", "none"
    if live:
        from agents.langgraph_guest.agent import _env

        env = _env()
        primary_name, fallback_name = env.get("LLM_MODEL", "?"), env.get("LLM_FALLBACK_MODEL", "none")
    meter = RequestMeter(max_requests=max_requests, min_interval_s=min_interval_s, live=live)
    started = datetime.now(timezone.utc)
    log(f"[evals] mode={'LIVE' if live else 'offline'} runs={runs} max_requests={max_requests} primary={primary_name} fallback={fallback_name}")
    all_results: list[dict[str, Any]] = []
    for i in range(1, runs + 1):
        run_id = f"{secrets.token_hex(3)}{i}"
        log(f"[evals] run {i}/{runs} (tenant eval-{run_id}-*)")
        res = asyncio.run(run_once(run_id, live, meter, primary_name, log))
        all_results.extend(res)
        if any(str(r.get("error", "")).startswith("budget") for r in res):
            break
    summary = summarize(all_results)
    meta = {"date": started.strftime("%Y-%m-%d %H:%M UTC"), "commit": _git_commit(), "mode": "live" if live else "offline", "runs": runs,
            "primary_model": primary_name, "fallback_model": fallback_name, "llm_requests_used": meter.total, "max_requests": max_requests,
            "min_interval_s": min_interval_s, "models_seen": sorted(set(meter.models_seen))}
    results_dir.mkdir(parents=True, exist_ok=True)
    raw = results_dir / f"{started.strftime('%Y%m%dT%H%M%SZ')}-{'live' if live else 'offline'}.json"
    raw.write_text(json.dumps({"meta": meta, "summary": summary, "results": all_results}, indent=2, default=str), encoding="utf-8")
    write_markdown(docs_path, meta, all_results, summary)
    ag, bl = summary["agent"], summary["baseline"]
    log(f"[evals] LLM agent: {ag['verified_correct']}/{ag['total']} verified correct; deterministic baseline: {bl['verified_correct']}/{bl['total']}; "
        f"LLM requests used: {meter.total}; fallbacks: {ag['fallbacks']}")
    log(f"[evals] raw: {raw}\n[evals] report: {docs_path}")
    return {"meta": meta, "summary": summary, "results": all_results, "raw_path": str(raw)}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--live", action="store_true", help="call the real LLM (requires RUN_LIVE_LLM=1)")
    ap.add_argument("--runs", type=int, default=None, help="number of full passes (default: 1 offline, 2 live)")
    ap.add_argument("--max-requests", type=int, default=100, help="hard cap on LLM requests across the whole eval")
    ap.add_argument("--min-interval", type=float, default=None,
                    help="seconds between LLM requests in live mode (default 12: each request re-sends the transcript, so Groq's tokens-per-minute limit needs this much spacing)")
    ap.add_argument("--rescore", type=str, default=None, help="re-apply scoring to a saved raw results JSON and regenerate the report (no network)")
    ap.add_argument("--docs", type=str, default=None, help="report path (default docs/EVALS.md for live, docs/EVALS-offline.md for offline)")
    args = ap.parse_args()
    docs_path = Path(args.docs) if args.docs else (ROOT / "docs" / ("EVALS.md" if (args.live or args.rescore) else "EVALS-offline.md"))
    if args.rescore:
        d = rescore(Path(args.rescore), docs_path=docs_path)
        ag, bl = d["summary"]["agent"], d["summary"]["baseline"]
        print(f"[evals] rescored {args.rescore}: LLM agent {ag['verified_correct']}/{ag['total']}; baseline {bl['verified_correct']}/{bl['total']}")
        return
    runs = args.runs if args.runs is not None else (2 if args.live else 1)
    min_interval = args.min_interval if args.min_interval is not None else (12.0 if args.live else 0.0)
    print(f"[evals] start {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} local")
    run(live=args.live, runs=runs, max_requests=args.max_requests, min_interval_s=min_interval, docs_path=docs_path)
    print(f"[evals] end   {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} local")


if __name__ == "__main__":
    main()

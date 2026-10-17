import { useState } from 'react'
import type { Report, Action } from '../api'
import { api } from '../api'
import { fmtDate, fmtTime, OUTCOME_LABEL, OUTCOME_TONE, CHECK_LABEL } from '../lib/format'

export default function Verdict({ report, onEvidence, onAction }: { report: Report; onEvidence: (ids: string[]) => void; onAction: (a: Action) => void }) {
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState<string | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const rc = report.root_cause
  const act = report.action
  const conf = report.confidence

  const approve = async () => {
    if (!act) return
    setBusy(true); setErr(null)
    try {
      const res = await api.approve(act.id)
      setMsg(res.message)
      onAction(res.action)
    } catch (e) { setErr((e as Error).message) } finally { setBusy(false) }
  }

  return (
    <div className="space-y-3 p-3">
      <div className={`fade-in rounded-lg border px-3 py-2 text-sm font-semibold ${OUTCOME_TONE[report.outcome]}`}>{OUTCOME_LABEL[report.outcome]}</div>

      {report.memory?.used && (
        <div className="fade-in rounded-lg border border-violet-500/40 bg-violet-500/10 p-3 text-[12.5px] text-violet-100">
          <div className="font-semibold">🧠 Answered from memory — learned {fmtDate(report.memory.learned_at)}</div>
          <div className="mt-1 text-violet-200/90">
            Investigation <span className="mono">{report.memory.investigation_id}</span> on incident <span className="mono">{report.memory.incident_id}</span> ("{report.memory.incident_title}") already established this root cause
            {report.memory.confirmed ? <span className="text-emerald-300"> and an operator approved the remediation</span> : <span className="text-amber-300"> (not yet approved by an operator)</span>}.
            Re-verified against this incident's timeline in <b>{report.tool_calls}</b> tool calls.
          </div>
        </div>
      )}

      {rc && (
        <div className="fade-in rounded-lg border border-rose-500/40 bg-rose-500/5 p-3">
          <div className="text-[11px] uppercase tracking-wider text-rose-300">Best-supported explanation</div>
          <div className="mt-1 text-[15px] font-semibold text-slate-100">
            <button className="hover:underline" onClick={() => onEvidence([rc.deployment_id, rc.service])}>{rc.deployment_id}</button>
            <span className="text-slate-400"> · {rc.service} {rc.version ?? ''} at {fmtTime(rc.deployment_at)}{rc.author ? ` by ${rc.author}` : ''}</span>
          </div>
          <ul className="mt-1.5 space-y-1">
            {rc.changes.map((c) => (
              <li key={c.id} className="text-[12.5px] text-slate-200">
                <span className="mono text-slate-400">{c.id}</span> — {c.summary}
                {c.risk && <span className={`ml-2 rounded border px-1 text-[10px] ${c.risk === 'high' ? 'border-rose-500/50 text-rose-300' : 'border-slate-600 text-slate-400'}`}>risk {c.risk}</span>}
                {c.files && <div className="mono text-[10.5px] text-slate-500">{c.files.join(', ')}</div>}
              </li>
            ))}
          </ul>
          {rc.path && rc.path.length > 1 && <div className="mt-2 text-[12px] text-slate-300">Dependency path: <span className="mono text-sky-300">{rc.path.join(' → ')}</span></div>}
          <div className="mt-2 rounded border border-slate-700 bg-black/30 p-2">
            <div className="flex items-baseline justify-between text-[12px]">
              <span className="text-slate-300">Confidence: <b className="text-slate-100">{conf.passed}/{conf.total} checks passed</b> ({Math.round(conf.share * 100)}%)</span>
              <span className="text-[10.5px] text-slate-500">{conf.note}</span>
            </div>
            <div className="mt-1.5 flex flex-wrap gap-1">
              {(conf.breakdown ?? []).map((b, i) => (
                <span key={i} className={`rounded border px-1.5 py-0.5 text-[10.5px] ${b.passed ? 'border-emerald-500/40 text-emerald-300' : b.passed === false ? 'border-rose-500/40 text-rose-300' : 'border-amber-500/40 text-amber-300'}`}>
                  {b.passed ? '✓' : b.passed === false ? '✗' : '?'} {CHECK_LABEL[b.check] ?? b.check}{b.hard ? ' *' : ''}
                </span>
              ))}
            </div>
            <div className="mt-1 text-[10px] text-slate-500">* hard check: a failure alone rules the hypothesis out</div>
          </div>
        </div>
      )}

      {report.outcome === 'inconclusive' && (
        <div className="fade-in space-y-2 rounded-lg border border-amber-500/40 bg-amber-500/5 p-3 text-[12.5px]">
          <div className="font-semibold text-amber-200">No candidate passed enough graph checks. Here is exactly what was checked:</div>
          <ul className="list-disc space-y-0.5 pl-5 text-slate-300">
            {report.checked.map((c) => <li key={c.step}><span className="mono text-sky-300">{c.tool}</span> — {c.summary} ({c.rows} rows)</li>)}
          </ul>
          <div className="pt-1 font-semibold text-amber-200">Missing data that would resolve it:</div>
          <ul className="list-disc space-y-0.5 pl-5 text-slate-300">{report.missing_data.map((m, i) => <li key={i}>{m}</li>)}</ul>
        </div>
      )}

      {report.similar_incidents.length > 0 && (
        <div className="fade-in rounded-lg border border-[var(--border)] bg-[var(--panel-2)] p-3">
          <div className="text-[11px] uppercase tracking-wider text-slate-400">Similar past incidents (matched by graph structure)</div>
          {report.similar_incidents.map((s) => (
            <div key={s.id} className="mt-1.5 text-[12.5px]">
              <button className="font-semibold text-slate-100 hover:underline" onClick={() => onEvidence([s.id, s.service, ...(s.reverted_deployment ? [s.reverted_deployment] : [])])}>{s.id}</button>
              <span className="text-slate-400"> · {s.title} · {fmtDate(s.opened_at)}</span>
              <div className="mt-0.5 flex flex-wrap gap-1">{s.match_links.map((m) => <span key={m} className="mono rounded border border-sky-500/40 px-1.5 text-[10px] text-sky-300">{m}</span>)}
                {s.reverted_files?.map((f) => <span key={f} className="mono rounded border border-violet-500/40 px-1.5 text-[10px] text-violet-300">CodeChange file: {f}</span>)}</div>
              {s.resolution_summary && <div className="mt-1 text-slate-300">Resolved: {s.resolution_summary}</div>}
              {s.runbook_id && <div className="text-slate-400">Runbook <span className="mono">{s.runbook_id}</span> — {s.runbook_title}</div>}
            </div>
          ))}
        </div>
      )}

      {act && (
        <div className="fade-in rounded-lg border border-sky-500/40 bg-sky-500/5 p-3">
          <div className="flex items-center justify-between">
            <div className="text-[11px] uppercase tracking-wider text-sky-300">Proposed remediation</div>
            <span className="rounded border border-amber-500/50 px-1.5 text-[10px] text-amber-300">SIMULATED</span>
          </div>
          <div className="mt-1 text-[13px] text-slate-100">{act.summary}</div>
          <div className="mono mt-1 text-[11px] text-slate-400">action {act.id} · {act.kind} · targets {act.deployment_id}{act.runbook_id ? ` · runbook ${act.runbook_id}` : ''}</div>
          {act.runbook_steps?.length > 0 && (
            <ol className="mt-2 list-decimal space-y-0.5 pl-5 text-[12px] text-slate-300">{act.runbook_steps.map((s, i) => <li key={i}>{s}</li>)}</ol>
          )}
          <div className="mt-3 flex items-center gap-3">
            {act.status === 'proposed' ? (
              <button onClick={approve} disabled={busy} className="rounded-md bg-emerald-500 px-3 py-1.5 text-sm font-semibold text-black hover:bg-emerald-400 disabled:opacity-50">
                {busy ? 'Recording…' : 'Approve rollback (simulated)'}
              </button>
            ) : (
              <span className="rounded-md border border-emerald-500/50 bg-emerald-500/10 px-3 py-1.5 text-sm font-semibold text-emerald-300">✔ {act.status} (simulated)</span>
            )}
            <span className="text-[11px] text-slate-500">Requires a human click. Writes an Action node; nothing real is rolled back.</span>
          </div>
          {msg && <div className="mt-2 text-[12px] text-emerald-300">{msg}</div>}
          {err && <div className="mt-2 text-[12px] text-rose-300">{err}</div>}
        </div>
      )}

      <div className="fade-in rounded-lg border border-[var(--border)] bg-[var(--panel-2)] p-3">
        <div className="flex items-center justify-between">
          <div className="text-[11px] uppercase tracking-wider text-slate-400">Narrative</div>
          <span className={`rounded border px-1.5 text-[10px] ${report.narrative_source === 'llm' ? 'border-violet-500/50 text-violet-300' : 'border-slate-600 text-slate-400'}`}>
            {report.narrative_source === 'llm' ? 'written by LLM · validated against evidence' : 'deterministic template'}
          </span>
        </div>
        <p className="mt-1.5 text-[12.5px] leading-relaxed text-slate-200">{report.narrative}</p>
        {report.llm_fallbacks.map((f, i) => (
          <div key={i} className="mt-2 rounded border border-amber-500/40 bg-amber-500/10 px-2 py-1 text-[11px] text-amber-200">LLM unavailable → deterministic ({f.step}): {f.reason}</div>
        ))}
      </div>
    </div>
  )
}

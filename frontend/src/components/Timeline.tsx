import { useState } from 'react'
import type { Report, TraceStep } from '../api'
import { fmtMs } from '../lib/format'

const TOOL_ICON: Record<string, string> = {
  get_incident: '◎', upstream_dependencies: '⇡', recall: '🧠', deployments_in_window: '▲', error_timeline: '⏱', critical_services: '◈',
  path_between: '⟿', healthy_siblings: '⚖', similar_incidents: '↺', record_investigation: '✎', record_action: '✔',
}

function Step({ t, onHover }: { t: TraceStep; onHover: (ids: string[] | null) => void }) {
  const [showCypher, setShowCypher] = useState(false)
  const [showRows, setShowRows] = useState(false)
  const writes = t.queries.some((q) => !q.read_only)
  return (
    <li className={`fade-in rounded-lg border p-2.5 ${t.status === 'error' ? 'border-rose-500/40 bg-rose-500/5' : 'border-[var(--border)] bg-[var(--panel-2)]'}`}
      onMouseEnter={() => onHover(t.evidence_ids)} onMouseLeave={() => onHover(null)}>
      <div className="flex items-start gap-2">
        <span className="mt-0.5 inline-flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-slate-800 text-[11px] text-slate-300">{t.step}</span>
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <span className="mono text-[13px] font-semibold text-sky-300">{TOOL_ICON[t.tool] ?? '•'} {t.tool}</span>
            {writes && <span className="rounded border border-amber-500/40 px-1.5 text-[10px] text-amber-300">WRITE</span>}
            {!writes && <span className="rounded border border-slate-600 px-1.5 text-[10px] text-slate-400">RO_QUERY</span>}
            <span className="ml-auto text-[11px] text-slate-400">{t.row_count} row{t.row_count === 1 ? '' : 's'} · {fmtMs(t.duration_ms)}</span>
          </div>
          {t.purpose && <div className="mt-0.5 text-[12px] text-slate-400">{t.purpose}</div>}
          <div className="mt-1 text-[12px] text-slate-200">{t.status === 'error' ? <span className="text-rose-300">{t.error}</span> : t.summary}</div>
          <div className="mt-1.5 flex flex-wrap gap-1.5">
            {Object.entries(t.args).filter(([k]) => k !== 'investigation').map(([k, v]) => (
              <span key={k} className="mono rounded bg-slate-800/70 px-1.5 py-0.5 text-[10px] text-slate-300">{k}={Array.isArray(v) ? `[${v.length}]` : String(v)}</span>
            ))}
          </div>
          <div className="mt-2 flex gap-3 text-[11px]">
            <button className="text-sky-400 hover:underline" onClick={() => setShowCypher((s) => !s)}>{showCypher ? 'Hide' : 'Show'} Cypher ({t.queries.length})</button>
            {t.rows_preview.length > 0 && <button className="text-slate-400 hover:underline" onClick={() => setShowRows((s) => !s)}>{showRows ? 'Hide' : 'Show'} rows</button>}
          </div>
          {showCypher && (
            <div className="mt-2 space-y-2">
              {t.queries.map((q, i) => (
                <div key={i} className="rounded border border-slate-700 bg-black/40 p-2">
                  <pre className="mono scroll-thin max-h-56 overflow-auto whitespace-pre-wrap text-[11px] leading-snug text-emerald-200">{q.cypher}</pre>
                  <div className="mt-1 flex items-center justify-between text-[10px] text-slate-500">
                    <span className="mono truncate">params: {JSON.stringify(q.params).slice(0, 300)}</span>
                    <span>{q.row_count} rows · {fmtMs(q.duration_ms)} · {q.read_only ? 'GRAPH.RO_QUERY' : 'GRAPH.QUERY'}</span>
                  </div>
                </div>
              ))}
            </div>
          )}
          {showRows && (
            <pre className="mono scroll-thin mt-2 max-h-56 overflow-auto rounded border border-slate-700 bg-black/40 p-2 text-[10.5px] leading-snug text-slate-300">
              {JSON.stringify(t.rows_preview, null, 1)}
            </pre>
          )}
        </div>
      </div>
    </li>
  )
}

export default function Timeline({ report, step, onHover }: { report: Report | null; step: number; onHover: (ids: string[] | null) => void }) {
  if (!report) {
    return <div className="flex h-full items-center justify-center p-6 text-center text-sm text-slate-500">Pick an incident and press <b className="mx-1 text-slate-300">Investigate</b> — every tool call, its Cypher and its rows will appear here.</div>
  }
  if (report.outcome === 'unknown_entity') {
    return (
      <div className="p-4 text-sm">
        <div className="rounded-lg border border-rose-500/40 bg-rose-500/10 p-3 text-rose-200">{report.error}</div>
        <p className="mt-3 text-slate-400">The name was resolved against the graph first and did not exist, so <b>no tool query ran</b> (0 steps).</p>
      </div>
    )
  }
  const shown = report.trace.slice(0, step)
  const stateAt = report.states
  return (
    <div className="p-3">
      <div className="mb-2 flex flex-wrap gap-1 text-[10px] text-slate-400">
        {stateAt.map((s) => <span key={s.state} className="rounded bg-slate-800 px-1.5 py-0.5">{s.state}</span>)}
      </div>
      <ol className="space-y-2">
        {shown.map((t) => <Step key={t.step} t={t} onHover={onHover} />)}
      </ol>
      {step < report.trace.length && <div className="pulse mt-3 text-center text-xs text-sky-400">running step {step + 1} of {report.trace.length}…</div>}
      {step >= report.trace.length && <div className="mt-3 text-center text-[11px] text-slate-500">{report.trace.length} tool calls · {fmtMs(report.duration_ms)} total · all queries above ran against FalkorDB</div>}
    </div>
  )
}

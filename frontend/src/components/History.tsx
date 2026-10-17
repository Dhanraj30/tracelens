import { useEffect, useState } from 'react'
import { fmtDate, fmtMs, OUTCOME_LABEL, OUTCOME_TONE } from '../lib/format'
import { u } from '../api'

interface Row { id: string; at: number; mode: string; outcome: string; tool_calls: number; confidence: number; incident_id: string; incident_title: string; action_id?: string; action_status?: string; agent: string; verified: boolean; supported: number; rejected: number }
interface Detail {
  id: string; at: number; mode: string; outcome: string; tool_calls: number; confidence: number; agent: string; verified: boolean
  trace: { step: number; tool: string; args: Record<string, unknown>; summary?: string; rows?: number; ms?: number; purpose?: string; status?: string }[]
  incident: { id: string; title: string; symptom: string; opened_at: number; service: string }
  action: { id: string; status: string; summary: string; deployment_id: string; runbook_id?: string } | null
  hypotheses: { id: string; status: string; score: number; kind: string; label: string; confirmed: boolean; suspects: string[]; supported_by: { check: string; target: string }[]; rejected_by: { check: string; target: string }[] }[]
  source?: string
}

async function j<T>(r: Response): Promise<T> { if (!r.ok) throw new Error(`${r.status}`); return r.json() as Promise<T> }

function AgentBadge({ agent, verified }: { agent: string; verified: boolean }) {
  const guest = agent !== 'engine'
  return (
    <span className={`mono rounded border px-1.5 py-0.5 text-[10px] ${guest ? 'border-violet-500/50 text-violet-300' : 'border-sky-500/50 text-sky-300'}`} title={guest ? 'written by an outside agent over MCP' : 'TraceLens engine'}>
      {agent}{guest && verified ? ' · server-verified' : ''}
    </span>
  )
}

export default function History({ refreshKey, onEvidence }: { refreshKey: number; onEvidence: (ids: string[]) => void }) {
  const [rows, setRows] = useState<Row[]>([])
  const [sel, setSel] = useState<string | null>(null)
  const [detail, setDetail] = useState<Detail | null>(null)
  const [err, setErr] = useState<string | null>(null)

  useEffect(() => { fetch(u('/api/investigations')).then(j<Row[]>).then(setRows).catch((e) => setErr((e as Error).message)) }, [refreshKey])
  useEffect(() => {
    if (!sel) { setDetail(null); return }
    fetch(u(`/api/investigations/${sel}`)).then(j<Detail>).then(setDetail).catch((e) => setErr((e as Error).message))
  }, [sel])

  if (err) return <div className="p-4 text-sm text-rose-300">{err}</div>
  if (!rows.length) return <div className="p-6 text-center text-sm text-slate-500">No investigations in the graph yet. Run one here, or from the MCP client / LangGraph guest, and it appears for every agent.</div>

  return (
    <div className="p-3">
      <div className="mb-2 text-[11px] text-slate-400">Every Investigation node in FalkorDB, whoever wrote it. Guest agents' traces carry their stated reasons, which are recorded but <b>unverified</b>; their suspects were re-checked by the server before being stored.</div>
      <ul className="space-y-1.5">
        {rows.map((r) => (
          <li key={r.id}>
            <button onClick={() => setSel(sel === r.id ? null : r.id)} className={`w-full rounded-lg border p-2 text-left hover:bg-slate-800/60 ${sel === r.id ? 'border-sky-500/50 bg-slate-800/60' : 'border-[var(--border)] bg-[var(--panel-2)]'}`}>
              <div className="flex flex-wrap items-center gap-1.5 text-[12px]">
                <AgentBadge agent={r.agent} verified={r.verified} />
                <span className={`rounded border px-1.5 py-0.5 text-[10px] ${OUTCOME_TONE[r.outcome] ?? 'border-slate-600 text-slate-300'}`}>{OUTCOME_LABEL[r.outcome] ?? r.outcome}</span>
                <span className="ml-auto text-[10.5px] text-slate-500">{fmtDate(r.at)}</span>
              </div>
              <div className="mt-1 text-[12px] text-slate-200"><span className="mono text-slate-400">{r.incident_id}</span> · {r.incident_title}</div>
              <div className="mt-0.5 text-[10.5px] text-slate-500">{r.tool_calls} tool calls · {r.supported} supported · {r.rejected} ruled out{r.action_status ? ` · action ${r.action_status}` : ''} · <span className="mono">{r.id}</span></div>
            </button>
            {sel === r.id && detail && (
              <div className="fade-in mt-1 rounded-lg border border-[var(--border)] bg-black/30 p-2.5 text-[12px]">
                <div className="mb-1.5 text-[11px] uppercase tracking-wider text-slate-400">Trace ({detail.trace.length} steps{detail.source === 'graph' ? ', from the graph' : ''})</div>
                <ol className="space-y-1">
                  {detail.trace.map((s, i) => (
                    <li key={i} className="rounded bg-slate-900/60 px-2 py-1">
                      <div className="flex items-baseline gap-2"><span className="w-5 text-right text-slate-500">{s.step ?? i + 1}</span>
                        <span className={`mono ${s.tool === 'server_verification' ? 'text-emerald-300' : 'text-sky-300'}`}>{s.tool}</span>
                        <span className="text-slate-400">{s.summary}</span>
                        <span className="ml-auto text-[10px] text-slate-500">{s.rows != null ? `${s.rows} rows` : ''} {s.ms != null ? `· ${fmtMs(s.ms)}` : ''}</span></div>
                      {s.purpose && <div className={`ml-7 text-[11px] ${s.purpose.startsWith("model's stated reason") ? 'text-amber-200/90 italic' : 'text-slate-500'}`}>{s.purpose}</div>}
                    </li>
                  ))}
                </ol>
                <div className="mb-1.5 mt-3 text-[11px] uppercase tracking-wider text-slate-400">Verdict</div>
                <ul className="space-y-1">
                  {detail.hypotheses.map((h) => (
                    <li key={h.id} className="rounded bg-slate-900/60 px-2 py-1">
                      <button className="text-left hover:underline" onClick={() => onEvidence([...h.suspects, ...h.supported_by.map((e) => e.target), ...h.rejected_by.map((e) => e.target)])}>
                        <span className={`mr-1.5 rounded border px-1 text-[10px] ${h.status === 'supported' ? 'border-emerald-500/50 text-emerald-300' : h.status === 'rejected' ? 'border-slate-500 text-slate-300' : 'border-amber-500/50 text-amber-300'}`}>{h.status.toUpperCase()}</span>
                        <span className={h.status === 'rejected' ? 'line-through text-slate-400' : 'text-slate-100'}>{h.label}</span>
                        {h.confirmed && <span className="ml-1 text-[10px] text-emerald-300">operator confirmed</span>}
                      </button>
                      <div className="mono mt-0.5 text-[10px] text-slate-500">
                        {h.supported_by.length > 0 && <span className="text-emerald-400/80">✓ {h.supported_by.map((e) => `${e.check}→${e.target}`).join(', ')} </span>}
                        {h.rejected_by.length > 0 && <span className="text-rose-400/80">✗ {h.rejected_by.map((e) => `${e.check}→${e.target}`).join(', ')}</span>}
                      </div>
                    </li>
                  ))}
                </ul>
                {detail.action && <div className="mt-2 text-[11.5px] text-slate-300">Action <span className="mono">{detail.action.id}</span> · {detail.action.status} · {detail.action.summary}</div>}
              </div>
            )}
          </li>
        ))}
      </ul>
    </div>
  )
}

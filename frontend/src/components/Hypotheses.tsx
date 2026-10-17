import type { Hypothesis, Check } from '../api'
import { CHECK_LABEL } from '../lib/format'

const STATUS: Record<string, { label: string; cls: string }> = {
  supported: { label: 'SUPPORTED', cls: 'border-emerald-500/50 text-emerald-300 bg-emerald-500/10' },
  rejected: { label: 'RULED OUT', cls: 'border-slate-500/50 text-slate-300 bg-slate-500/10' },
  inconclusive: { label: 'INCONCLUSIVE', cls: 'border-amber-500/50 text-amber-300 bg-amber-500/10' },
  pending: { label: 'PENDING', cls: 'border-slate-600 text-slate-400' },
}

function Mark({ c }: { c: Check }) {
  if (c.passed === true) return <span className="text-emerald-400">✓</span>
  if (c.passed === false) return <span className="text-rose-400">✗</span>
  return <span className="text-amber-300">?</span>
}

export default function Hypotheses({ hyps, onEvidence, selected }: { hyps: Hypothesis[]; onEvidence: (ids: string[]) => void; selected: string[] }) {
  if (!hyps.length) return <div className="p-4 text-sm text-slate-500">No hypotheses yet.</div>
  return (
    <div className="space-y-2 p-3">
      {hyps.map((h) => {
        const st = STATUS[h.status] ?? STATUS.pending
        const rejected = h.status === 'rejected'
        return (
          <div key={h.id} className={`fade-in rounded-lg border p-3 ${h.status === 'supported' ? 'border-emerald-500/40 bg-emerald-500/5' : 'border-[var(--border)] bg-[var(--panel-2)]'}`}>
            <div className="flex items-start gap-2">
              <span className={`rounded border px-1.5 py-0.5 text-[10px] font-semibold tracking-wide ${st.cls}`}>{st.label}</span>
              <div className={`min-w-0 flex-1 text-[13px] ${rejected ? 'line-through text-slate-400' : 'text-slate-100'}`}>{h.label}</div>
              <div className="shrink-0 text-right">
                <div className="text-[11px] text-slate-400">checks passed</div>
                <div className="mono text-sm">{h.checks.filter((c) => c.passed === true).length}/{h.checks.filter((c) => c.passed !== null).length}</div>
              </div>
            </div>
            <div className="mt-1 h-1.5 w-full overflow-hidden rounded bg-slate-800">
              <div className={`h-full ${h.status === 'supported' ? 'bg-emerald-400' : h.status === 'rejected' ? 'bg-slate-500' : 'bg-amber-400'}`} style={{ width: `${Math.round(h.score * 100)}%` }} />
            </div>
            <ul className="mt-2 space-y-1">
              {h.checks.map((c, i) => {
                const active = c.evidence_ids.length > 0 && c.evidence_ids.every((e) => selected.includes(e)) && selected.length === c.evidence_ids.length
                return (
                  <li key={i}>
                    <button onClick={() => onEvidence(c.evidence_ids)} title="Highlight this evidence in the graph"
                      className={`w-full rounded px-1.5 py-1 text-left text-[12px] leading-snug hover:bg-slate-800/70 ${active ? 'bg-slate-800 ring-1 ring-slate-500' : ''}`}>
                      <span className="mr-1.5 inline-block w-3 text-center"><Mark c={c} /></span>
                      <span className="font-medium text-slate-200">{CHECK_LABEL[c.name] ?? c.name}</span>
                      {c.hard && <span className="ml-1 rounded border border-slate-600 px-1 text-[9px] text-slate-400">hard</span>}
                      <span className="text-slate-400"> — {c.detail}</span>
                      {c.evidence_ids.length > 0 && <span className="mono ml-1 text-[10px] text-slate-500">[{c.evidence_ids.slice(0, 4).join(', ')}{c.evidence_ids.length > 4 ? ', …' : ''}]</span>}
                    </button>
                  </li>
                )
              })}
            </ul>
          </div>
        )
      })}
    </div>
  )
}

export const fmtTime = (ts?: number | null) =>
  ts == null ? '—' : new Date(ts * 1000).toISOString().slice(11, 16) + ' UTC'

export const fmtDate = (ts?: number | null) =>
  ts == null ? '—' : new Date(ts * 1000).toLocaleString('en-GB', { day: '2-digit', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit', timeZone: 'UTC' }) + ' UTC'

export const fmtMs = (ms?: number) => (ms == null ? '' : ms < 1 ? '<1 ms' : `${ms.toFixed(1)} ms`)

export const OUTCOME_LABEL: Record<string, string> = {
  root_cause_supported: 'Best-supported explanation found',
  multiple_candidates: 'Multiple candidates remain',
  inconclusive: 'Inconclusive — evidence incomplete',
  answered_from_memory: 'Answered from memory',
  unknown_entity: 'Unknown entity — no queries run',
}

export const OUTCOME_TONE: Record<string, string> = {
  root_cause_supported: 'text-emerald-300 border-emerald-500/40 bg-emerald-500/10',
  multiple_candidates: 'text-amber-300 border-amber-500/40 bg-amber-500/10',
  inconclusive: 'text-amber-300 border-amber-500/40 bg-amber-500/10',
  answered_from_memory: 'text-violet-300 border-violet-500/40 bg-violet-500/10',
  unknown_entity: 'text-rose-300 border-rose-500/40 bg-rose-500/10',
}

export const CHECK_LABEL: Record<string, string> = {
  temporal_precedence: 'Temporal precedence',
  path: 'Dependency path',
  origin: 'Error origin',
  common_cause: 'Common-cause test',
  history: 'History (shared signature)',
  memory: 'Memory',
}

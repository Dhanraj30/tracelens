import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { api, type Action, type GraphView, type Health, type Incident, type Report } from './api'
import DependencyGraph, { computeHighlight } from './components/DependencyGraph'
import Timeline from './components/Timeline'
import Hypotheses from './components/Hypotheses'
import Verdict from './components/Verdict'
import { fmtTime } from './lib/format'

const STEP_MS = 700

export default function App() {
  const [health, setHealth] = useState<Health | null>(null)
  const [incidents, setIncidents] = useState<Incident[]>([])
  const [graph, setGraph] = useState<GraphView | null>(null)
  const [selected, setSelected] = useState<string>('inc-1042')
  const [mode, setMode] = useState<'deterministic' | 'llm'>('deterministic')
  const [report, setReport] = useState<Report | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [step, setStep] = useState(0)
  const [playing, setPlaying] = useState(true)
  const [evidence, setEvidence] = useState<string[]>([])
  const [hover, setHover] = useState<string[] | null>(null)
  const [rightTab, setRightTab] = useState<'hypotheses' | 'verdict'>('hypotheses')
  const timer = useRef<number | null>(null)

  const refresh = useCallback(async () => {
    try {
      const [h, inc, g] = await Promise.all([api.health(), api.incidents(), api.graph()])
      setHealth(h); setIncidents(inc); setGraph(g); setError(null)
    } catch (e) { setError(`Backend unreachable: ${(e as Error).message}`) }
  }, [])
  useEffect(() => { refresh() }, [refresh])

  // ?incident=inc-1042&instant=1  -> auto-run on load (used for QA screenshots and the demo script)
  const autoRan = useRef(false)
  useEffect(() => {
    if (autoRan.current || !health?.falkordb.reachable) return
    const q = new URLSearchParams(window.location.search)
    const id = q.get('incident')
    if (!id) return
    autoRan.current = true
    setSelected(id)
    const m = q.get('mode') === 'llm' ? 'llm' : 'deterministic'
    setLoading(true); setReport(null); setStep(0); setEvidence([])
    if (q.get('tab') === 'hypotheses') preferredTab.current = 'hypotheses'
    api.investigate(id, m).then((r) => {
      setReport(r)
      const fixed = q.get('step')
      if (fixed) { setStep(Math.min(Number(fixed), r.trace.length)); setPlaying(false) }
      else if (q.get('instant')) { setStep(r.trace.length); setPlaying(false) }
      else { setPlaying(true) }
      refresh()
    }).catch((e) => setError((e as Error).message)).finally(() => setLoading(false))
  }, [health, refresh])

  // replay the trace step by step
  useEffect(() => {
    if (timer.current) window.clearInterval(timer.current)
    if (!report || !playing) return
    timer.current = window.setInterval(() => {
      setStep((s) => {
        if (s >= report.trace.length) { setPlaying(false); return s }
        return s + 1
      })
    }, STEP_MS)
    return () => { if (timer.current) window.clearInterval(timer.current) }
  }, [report, playing])

  const done = !!report && step >= report.trace.length
  const preferredTab = useRef<'hypotheses' | 'verdict' | null>(null)
  useEffect(() => { if (done) setRightTab(preferredTab.current ?? 'verdict') }, [done, report])

  const investigate = async () => {
    setLoading(true); setError(null); setReport(null); setStep(0); setEvidence([]); setRightTab('hypotheses')
    try {
      const r = await api.investigate(selected, mode)
      setReport(r); setPlaying(true)
      refresh()
    } catch (e) { setError((e as Error).message) } finally { setLoading(false) }
  }

  const reset = async (memoryOnly: boolean) => {
    if (!memoryOnly && !window.confirm('Reset the demo graph? This re-seeds all data and clears agent memory.')) return
    setLoading(true)
    try { await api.reset(memoryOnly); setReport(null); setStep(0); await refresh() } catch (e) { setError((e as Error).message) } finally { setLoading(false) }
  }

  const onAction = (a: Action) => { if (report?.action) setReport({ ...report, action: { ...report.action, ...a } }); refresh() }

  const hl = useMemo(() => computeHighlight(report, graph, step, hover ?? evidence), [report, graph, step, hover, evidence])
  const inc = incidents.find((i) => i.id === selected)
  const visibleHyps = useMemo(() => (report && done ? report.hypotheses : []), [report, done])

  return (
    <div className="flex h-full flex-col">
      {/* header */}
      <header className="flex items-center gap-3 border-b border-[var(--border)] bg-[var(--panel)] px-4 py-2">
        <div className="flex items-center gap-2">
          <span className="inline-flex h-7 w-7 items-center justify-center rounded-md bg-sky-500/20 text-sky-300">◎</span>
          <div>
            <div className="text-[15px] font-bold leading-tight">TraceLens</div>
            <div className="text-[10.5px] leading-tight text-slate-400">symptom → root cause, proven by graph traversal on FalkorDB</div>
          </div>
        </div>
        <div className="mx-2 h-6 w-px bg-[var(--border)]" />
        <select value={selected} onChange={(e) => setSelected(e.target.value)} className="rounded-md border border-[var(--border)] bg-[var(--panel-2)] px-2 py-1.5 text-sm">
          {incidents.filter((i) => i.status === 'open').map((i) => <option key={i.id} value={i.id}>{i.id} — {i.title}</option>)}
          <optgroup label="resolved (history)">
            {incidents.filter((i) => i.status !== 'open').map((i) => <option key={i.id} value={i.id}>{i.id} — {i.title}</option>)}
          </optgroup>
        </select>
        <button onClick={investigate} disabled={loading || !health?.falkordb.reachable} className="rounded-md bg-sky-500 px-3 py-1.5 text-sm font-semibold text-black hover:bg-sky-400 disabled:opacity-50">
          {loading ? 'Investigating…' : 'Investigate'}
        </button>
        <div className="flex items-center overflow-hidden rounded-md border border-[var(--border)] text-[12px]">
          <button onClick={() => setMode('deterministic')} className={`px-2.5 py-1.5 ${mode === 'deterministic' ? 'bg-slate-700 text-white' : 'text-slate-400 hover:bg-slate-800'}`}>Deterministic</button>
          <button onClick={() => setMode('llm')} title={health?.llm.configured ? `LLM: ${health.llm.model}` : 'No LLM key configured — will fall back to deterministic'} className={`px-2.5 py-1.5 ${mode === 'llm' ? 'bg-violet-700 text-white' : 'text-slate-400 hover:bg-slate-800'}`}>
            LLM{health && !health.llm.configured ? ' (no key)' : ''}
          </button>
        </div>
        {report && (
          <div className="flex items-center gap-1 text-[12px]">
            <button onClick={() => setPlaying((p) => !p)} className="rounded border border-[var(--border)] px-2 py-1 hover:bg-slate-800">{playing && !done ? '❚❚' : '▶'}</button>
            <button onClick={() => { setPlaying(false); setStep((s) => Math.min(s + 1, report.trace.length)) }} className="rounded border border-[var(--border)] px-2 py-1 hover:bg-slate-800">step</button>
            <button onClick={() => { setPlaying(false); setStep(report.trace.length) }} className="rounded border border-[var(--border)] px-2 py-1 hover:bg-slate-800">skip ⏭</button>
            <button onClick={() => { setStep(0); setPlaying(true) }} className="rounded border border-[var(--border)] px-2 py-1 hover:bg-slate-800">replay</button>
            <span className="mono ml-1 text-slate-400">{Math.min(step, report.trace.length)}/{report.trace.length}</span>
          </div>
        )}
        <div className="ml-auto flex items-center gap-2 text-[11.5px]">
          <span className={`rounded border px-2 py-0.5 ${report?.mode === 'llm' ? 'border-violet-500/50 text-violet-300' : 'border-slate-600 text-slate-300'}`}>mode: {report?.mode ?? mode}</span>
          <span className={`flex items-center gap-1 rounded border px-2 py-0.5 ${health?.falkordb.reachable ? 'border-emerald-500/40 text-emerald-300' : 'border-rose-500/40 text-rose-300'}`}>
            <span className={`inline-block h-2 w-2 rounded-full ${health?.falkordb.reachable ? 'bg-emerald-400' : 'bg-rose-400'}`} />
            FalkorDB {health ? (health.falkordb.reachable ? `· ${health.graph.nodes} nodes` : 'unreachable') : '…'}
          </span>
          <button onClick={() => reset(true)} className="rounded border border-[var(--border)] px-2 py-0.5 text-slate-300 hover:bg-slate-800" title="Delete Investigation / Hypothesis / Action nodes written by the agent">forget memory</button>
          <button onClick={() => reset(false)} className="rounded border border-[var(--border)] px-2 py-0.5 text-slate-300 hover:bg-slate-800">reset demo</button>
        </div>
      </header>

      {error && <div className="border-b border-rose-500/40 bg-rose-500/10 px-4 py-1.5 text-sm text-rose-200">{error}</div>}

      {/* body */}
      <main className="grid min-h-0 flex-1 grid-cols-12 gap-3 p-3">
        {/* left: incident + timeline */}
        <section className="panel col-span-3 flex min-h-0 flex-col">
          <div className="border-b border-[var(--border)] p-3">
            <div className="text-[11px] uppercase tracking-wider text-slate-400">Incident</div>
            {inc ? (
              <>
                <div className="mt-0.5 text-[14px] font-semibold">{inc.title}</div>
                <div className="text-[12px] text-slate-300">{inc.symptom}</div>
                <div className="mt-1 flex flex-wrap gap-1.5 text-[11px]">
                  <span className="rounded border border-pink-500/40 px-1.5 text-pink-300">affects {inc.service}</span>
                  <span className="rounded border border-slate-600 px-1.5 text-slate-300">opened {fmtTime(inc.opened_at)}</span>
                  <span className="rounded border border-slate-600 px-1.5 text-slate-300">{inc.status}</span>
                  {(inc.investigations ?? 0) > 0 && <span className="rounded border border-violet-500/40 px-1.5 text-violet-300">{inc.investigations} prior investigation{inc.investigations === 1 ? '' : 's'}</span>}
                </div>
              </>
            ) : <div className="text-sm text-slate-500">{health?.graph.seeded === false ? 'Graph is empty — press "reset demo" to seed.' : 'Loading…'}</div>}
          </div>
          <div className="flex items-center justify-between px-3 pt-2 text-[11px] uppercase tracking-wider text-slate-400">
            <span>Investigation timeline</span>
            {report && <span className="normal-case tracking-normal text-slate-500">{report.window ? `window ${fmtTime(report.window.from)}–${fmtTime(report.window.to)}` : ''}</span>}
          </div>
          <div className="scroll-thin min-h-0 flex-1 overflow-auto">
            <Timeline report={report} step={step} onHover={setHover} />
          </div>
        </section>

        {/* center: graph */}
        <section className="panel col-span-5 relative min-h-0 overflow-hidden">
          <div className="absolute left-3 top-2 z-10 flex flex-wrap gap-1.5 text-[10.5px]">
            <span className="rounded border border-pink-500/50 bg-black/50 px-1.5 text-pink-300">symptom</span>
            <span className="rounded border border-sky-500/50 bg-black/50 px-1.5 text-sky-300">visited by traversal</span>
            <span className="rounded border border-amber-500/50 bg-black/50 px-1.5 text-amber-300">suspect</span>
            <span className="rounded border border-slate-500 bg-black/50 px-1.5 text-slate-300 line-through">ruled out</span>
            <span className="rounded border border-rose-500/60 bg-black/50 px-1.5 text-rose-300">root cause path</span>
            <span className="rounded border border-emerald-500/50 bg-black/50 px-1.5 text-emerald-300">healthy sibling</span>
            <span className="rounded border border-dashed border-violet-500/60 bg-black/50 px-1.5 text-violet-300">external boundary</span>
          </div>
          {graph ? <DependencyGraph graph={graph} hl={hl} /> : <div className="flex h-full items-center justify-center text-slate-500">loading graph…</div>}
          {loading && <div className="absolute inset-0 flex items-center justify-center bg-black/40 text-sm text-sky-300">running graph queries…</div>}
        </section>

        {/* right: hypotheses / verdict */}
        <section className="panel col-span-4 flex min-h-0 flex-col">
          <div className="flex border-b border-[var(--border)] text-[12px]">
            <button onClick={() => setRightTab('hypotheses')} className={`px-3 py-2 ${rightTab === 'hypotheses' ? 'border-b-2 border-sky-400 text-white' : 'text-slate-400'}`}>
              Hypotheses {visibleHyps.length ? `(${visibleHyps.filter((h) => h.status === 'supported').length} supported · ${visibleHyps.filter((h) => h.status === 'rejected').length} ruled out)` : ''}
            </button>
            <button onClick={() => setRightTab('verdict')} className={`px-3 py-2 ${rightTab === 'verdict' ? 'border-b-2 border-sky-400 text-white' : 'text-slate-400'}`}>Root cause & remediation</button>
            {evidence.length > 0 && <button onClick={() => setEvidence([])} className="ml-auto px-3 text-[11px] text-slate-400 hover:text-white">clear highlight ✕</button>}
          </div>
          <div className="scroll-thin min-h-0 flex-1 overflow-auto">
            {!report && <div className="p-6 text-center text-sm text-slate-500">Hypotheses are generated from deployments on the dependency path, then each is tested with graph checks. Ruled-out suspects stay visible — with the evidence that ruled them out.</div>}
            {report && !done && <div className="p-6 text-center text-sm text-slate-500 pulse">gathering evidence… ({Math.min(step, report.trace.length)}/{report.trace.length} tool calls)</div>}
            {report && done && rightTab === 'hypotheses' && <Hypotheses hyps={visibleHyps} onEvidence={setEvidence} selected={evidence} />}
            {report && done && rightTab === 'verdict' && <Verdict report={report} onEvidence={(ids) => { setEvidence(ids) }} onAction={onAction} />}
          </div>
        </section>
      </main>
    </div>
  )
}

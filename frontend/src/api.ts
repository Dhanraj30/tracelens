// Types mirror backend/agent/engine.py report + backend/tools/base.py ToolCall.

export interface Incident {
  id: string
  title: string
  status: string
  opened_at: number
  symptom: string
  service: string
  investigations?: number
}

export interface GraphService { name: string; tier: string; team: string; external: boolean; error_events: number }
export interface GraphEdge { source: string; target: string; kind: 'sync' | 'async'; weight: number }
export interface GraphDeployment { id: string; at: number; version: string; service: string }
export interface GraphView { services: GraphService[]; edges: GraphEdge[]; deployments: GraphDeployment[] }

export interface QueryRecord { cypher: string; params: Record<string, unknown>; duration_ms: number; row_count: number; read_only: boolean; error?: string | null }

export interface TraceStep {
  step: number
  tool: string
  args: Record<string, unknown>
  queries: QueryRecord[]
  duration_ms: number
  row_count: number
  status: 'ok' | 'error'
  error?: string | null
  summary: string
  purpose: string
  evidence_ids: string[]
  paths: string[][]
  rows_preview: Record<string, unknown>[]
}

export interface Check { name: string; passed: boolean | null; detail: string; evidence_ids: string[]; hard: boolean; tool_step?: number | null }

export interface Hypothesis {
  id: string
  kind: 'deployment' | 'shared_dependency' | 'memory'
  label: string
  suspect: string | null
  service: string
  status: 'supported' | 'rejected' | 'inconclusive' | 'pending'
  score: number
  checks: Check[]
  details: Record<string, unknown>
}

export interface SimilarIncident {
  id: string; title: string; opened_at: number; service: string; shared_signatures: string[]; same_service: boolean
  resolution_id?: string; resolution_summary?: string; runbook_id?: string; runbook_title?: string; runbook_steps?: string[]
  reverted_deployment?: string; reverted_files?: string[]; match_links: string[]
}

export interface Action {
  id: string; kind: string; status: 'proposed' | 'approved' | 'executed'; simulated: boolean; summary: string
  deployment_id: string; service: string; runbook_id?: string | null; runbook_title?: string | null; runbook_steps: string[]; executed_at?: number
}

export interface Report {
  id: string
  at: number
  mode: 'deterministic' | 'llm'
  outcome: 'root_cause_supported' | 'multiple_candidates' | 'inconclusive' | 'answered_from_memory' | 'unknown_entity'
  incident: Incident & { observed?: { id: string; at: number; message: string; count: number; signature: string }[] }
  window: { from: number; to: number } | null
  hypotheses: Hypothesis[]
  root_cause: { deployment_id: string; version?: string; author?: string; deployment_at: number; service: string; changes: { id: string; summary: string; files?: string[]; risk?: string }[]; path: string[] | null; hypothesis_id: string } | null
  similar_incidents: SimilarIncident[]
  action: Action | null
  memory: { used: boolean; investigation_id: string; incident_id: string; incident_title: string; learned_at: number; confirmed: boolean; action_status?: string; agent?: string; verified?: boolean } | null
  missing_data: string[]
  checked: { step: number; tool: string; summary: string; rows: number }[]
  confidence: { passed: number; total: number; share: number; note: string; breakdown?: { check: string; passed: boolean | null; hard: boolean }[] }
  narrative: string
  narrative_source: 'template' | 'llm'
  llm_model?: string
  llm_fallbacks: { step: string; reason: string; fallback: string }[]
  graph_highlights: { visited?: string[]; suspects?: string[]; rejected?: string[]; root_path?: string[]; boundary?: string[]; healthy?: string[] }
  states: { state: string; at_ms: number; note: string }[]
  trace: TraceStep[]
  tool_calls: number
  duration_ms?: number
  error?: string
  first_symptom_at?: number | null
  origin?: { service: string | null; first_seen: number | null; signature: string | null }
  deployments?: { id: string; at: number; version: string; author: string; service: string; changes: { id: string; summary: string; risk: string }[] }[]
}

export interface Health {
  status: string
  falkordb: { reachable: boolean; host: string; port: number }
  graph: { name: string; nodes: number; seeded: boolean }
  llm: { configured: boolean; model: string | null; fallback_model?: string | null }
}

async function j<T>(res: Response): Promise<T> {
  if (!res.ok) {
    let detail = res.statusText
    try { detail = (await res.json()).detail ?? detail } catch { /* ignore */ }
    throw new Error(`${res.status}: ${detail}`)
  }
  return res.json() as Promise<T>
}

export const api = {
  health: () => fetch('/api/health').then(j<Health>),
  incidents: () => fetch('/api/incidents').then(j<Incident[]>),
  graph: () => fetch('/api/graph').then(j<GraphView>),
  investigate: (incident_id: string, mode: 'deterministic' | 'llm') =>
    fetch('/api/investigations', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ incident_id, mode }) }).then(j<Report>),
  approve: (actionId: string) => fetch(`/api/actions/${actionId}/approve`, { method: 'POST' }).then(j<{ action: Action; simulated: boolean; message: string }>),
  reset: (memoryOnly: boolean) => fetch(`/api/demo/reset?memory_only=${memoryOnly}`, { method: 'POST' }).then(j<unknown>),
}

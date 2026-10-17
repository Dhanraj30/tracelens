import { useMemo } from 'react'
import { ReactFlow, Background, Controls, MarkerType, Handle, Position, type Node, type Edge, type NodeProps } from '@xyflow/react'
import dagre from '@dagrejs/dagre'
import type { GraphView, Report } from '../api'

export interface Highlight {
  affected?: string
  visited: Set<string>
  visitedEdges: Set<string>
  suspects: Set<string>
  rejected: Set<string>
  root: Set<string>
  rootEdges: Set<string>
  healthy: Set<string>
  boundary: Set<string>
  evidence: Set<string>
  deployments: Map<string, 'suspect' | 'rejected' | 'root'>
  errors: Set<string>
  active: boolean
}

type SvcData = { name: string; tier: string; team: string; cls: string; chips: { text: string; cls: string }[] }

function ServiceNode({ data }: NodeProps<Node<SvcData>>) {
  return (
    <div className={`svc-node ${data.cls}`}>
      <Handle type="target" position={Position.Top} style={{ opacity: 0 }} />
      <div className="name">{data.name}</div>
      <div className="tier">{data.tier} · {data.team}</div>
      {data.chips.length > 0 && (
        <div>{data.chips.map((c, i) => <span key={i} className={`chip ${c.cls}`}>{c.text}</span>)}</div>
      )}
      <Handle type="source" position={Position.Bottom} style={{ opacity: 0 }} />
    </div>
  )
}

const nodeTypes = { svc: ServiceNode }
const W = 150, H = 62

export function edgeKey(a: string, b: string) { return `${a}->${b}` }

export function computeHighlight(report: Report | null, graph: GraphView | null, step: number, evidence: string[]): Highlight {
  const h: Highlight = {
    visited: new Set(), visitedEdges: new Set(), suspects: new Set(), rejected: new Set(), root: new Set(), rootEdges: new Set(),
    healthy: new Set(), boundary: new Set(), evidence: new Set(evidence), deployments: new Map(), errors: new Set(), active: !!report,
  }
  if (!report || !graph) return h
  const services = new Set(graph.services.map((s) => s.name))
  h.affected = report.incident?.service
  if (h.affected) h.visited.add(h.affected)
  const shown = report.trace.slice(0, step)
  for (const t of shown) {
    for (const id of t.evidence_ids) if (services.has(id)) h.visited.add(id)
    for (const p of t.paths) for (let i = 0; i + 1 < p.length; i++) h.visitedEdges.add(edgeKey(p[i], p[i + 1]))
    if (t.tool === 'deployments_in_window') for (const r of t.rows_preview) { const s = r.service as string; if (s) h.deployments.set(s, 'suspect') }
    if (t.tool === 'error_timeline') for (const r of t.rows_preview) { const s = r.service as string; if (s) h.errors.add(s) }
  }
  const done = step >= report.trace.length
  if (done) {
    const g = report.graph_highlights || {}
    for (const s of g.suspects || []) h.suspects.add(s)
    for (const s of g.rejected || []) { h.rejected.add(s); h.suspects.delete(s); if (h.deployments.has(s)) h.deployments.set(s, 'rejected') }
    for (const s of g.root_path || []) h.root.add(s)
    const rp = g.root_path || []
    for (let i = 0; i + 1 < rp.length; i++) h.rootEdges.add(edgeKey(rp[i], rp[i + 1]))
    for (const s of g.healthy || []) h.healthy.add(s)
    for (const s of g.boundary || []) h.boundary.add(s)
    if (report.root_cause) { h.deployments.set(report.root_cause.service, 'root'); h.root.add(report.root_cause.service) }
  }
  return h
}

export default function DependencyGraph({ graph, hl }: { graph: GraphView | null; hl: Highlight }) {
  const { nodes, edges } = useMemo(() => {
    if (!graph) return { nodes: [] as Node<SvcData>[], edges: [] as Edge[] }
    const g = new dagre.graphlib.Graph()
    g.setDefaultEdgeLabel(() => ({}))
    g.setGraph({ rankdir: 'TB', nodesep: 28, ranksep: 70, marginx: 10, marginy: 10 })
    for (const s of graph.services) g.setNode(s.name, { width: W, height: H })
    for (const e of graph.edges) g.setEdge(e.source, e.target)
    dagre.layout(g)

    const nodes: Node<SvcData>[] = graph.services.map((s) => {
      const p = g.node(s.name)
      const classes: string[] = []
      const chips: { text: string; cls: string }[] = []
      if (hl.active) {
        const isVisited = hl.visited.has(s.name)
        if (!isVisited && s.name !== hl.affected) classes.push('dim')
        if (isVisited) classes.push('visited')
        if (s.name === hl.affected) classes.push('affected')
        if (hl.suspects.has(s.name)) classes.push('suspect')
        if (hl.rejected.has(s.name)) classes.push('rejected')
        if (hl.healthy.has(s.name)) classes.push('healthy')
        if (hl.boundary.has(s.name)) classes.push('boundary')
        if (hl.root.has(s.name) && hl.deployments.get(s.name) === 'root') classes.push('root')
        if (hl.evidence.has(s.name)) classes.push('evidence')
        const dep = hl.deployments.get(s.name)
        if (dep === 'suspect') chips.push({ text: '▲ deploy · suspect', cls: 'deploy' })
        if (dep === 'rejected') chips.push({ text: '▲ deploy · ruled out', cls: 'ruled' })
        if (dep === 'root') chips.push({ text: '▲ deploy · root cause', cls: 'err' })
        if (hl.errors.has(s.name)) chips.push({ text: 'errors', cls: 'err' })
        if (hl.healthy.has(s.name)) chips.push({ text: 'healthy', cls: 'ok' })
        if (hl.boundary.has(s.name)) chips.push({ text: 'external · no data', cls: '' })
        if (s.name === hl.affected) chips.push({ text: 'symptom', cls: 'err' })
      } else if (s.external) {
        chips.push({ text: 'external', cls: '' })
      }
      return {
        id: s.name, type: 'svc', position: { x: p.x - W / 2, y: p.y - H / 2 },
        data: { name: s.name, tier: s.tier, team: s.team, cls: classes.join(' '), chips }, draggable: true,
      }
    })

    const edges: Edge[] = graph.edges.map((e) => {
      const k = edgeKey(e.source, e.target)
      const cls = [e.kind === 'async' ? 'async' : '']
      if (hl.active) {
        if (hl.rootEdges.has(k)) cls.push('root')
        else if (hl.visitedEdges.has(k)) cls.push('visited')
        else cls.push('dim')
      }
      return {
        id: k, source: e.source, target: e.target, className: cls.join(' '),
        markerEnd: { type: MarkerType.ArrowClosed, color: hl.rootEdges.has(k) ? '#f87171' : hl.visitedEdges.has(k) ? '#38bdf8' : '#334155', width: 14, height: 14 },
        label: e.kind === 'async' ? 'async' : undefined, labelStyle: { fill: '#64748b', fontSize: 9 }, labelBgStyle: { fill: '#0f172a' },
      }
    })
    return { nodes, edges }
  }, [graph, hl])

  return (
    <div className="h-full w-full">
      <ReactFlow nodes={nodes} edges={edges} nodeTypes={nodeTypes} fitView fitViewOptions={{ padding: 0.15 }} proOptions={{ hideAttribution: true }}
        nodesConnectable={false} elementsSelectable={false} minZoom={0.3} maxZoom={1.6}>
        <Background color="#1e293b" gap={24} />
        <Controls showInteractive={false} position="bottom-right" />
      </ReactFlow>
    </div>
  )
}

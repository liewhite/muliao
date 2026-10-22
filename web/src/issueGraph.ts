// issue 看板的依赖图视图：把 depends_on 排成有向无环图（依赖在左、依赖它的在右）的纯布局逻辑

import { Graph, layout, type Point } from '@dagrejs/dagre'
import type { Issue } from './api'
import { isFinished } from './issues'

/** 节点（卡片）的尺寸，渲染与布局共用 */
export const NODE_WIDTH = 224
export const NODE_HEIGHT = 72

/** 图里显示哪些 issue：open＝至少有一个没完成（完成/取消以外）的依赖链；all＝全部有依赖关系的 */
export type GraphScope = 'open' | 'all'

export interface GraphNode {
  issue: Issue
  /** 卡片左上角 */
  x: number
  y: number
}

export interface GraphEdge {
  /** 被依赖的 issue（先做的） */
  from: number
  /** 依赖它的 issue */
  to: number
  /** 依赖已完成或取消：不再挡着下游 */
  settled: boolean
  points: Point[]
}

export interface GraphLayout {
  nodes: GraphNode[]
  edges: GraphEdge[]
  width: number
  height: number
}

/** 已加载的 issue 之间的依赖边（依赖指向未加载的 issue，如按项目筛选掉了的，不画） */
function dependencyEdges(issues: Issue[]): [number, number][] {
  const ids = new Set(issues.map((i) => i.id))
  return issues.flatMap((i) => i.depends_on.filter((d) => ids.has(d)).map((d): [number, number] => [d, i.id]))
}

/** 按依赖关系连通的分组（无向），每组是 issue id 的列表；没有任何依赖关系的 issue 不在其中 */
export function dependencyGroups(issues: Issue[]): number[][] {
  const parent = new Map<number, number>()
  const find = (x: number): number => {
    let r = x
    while (parent.get(r) !== r) r = parent.get(r)!
    parent.set(x, r)
    return r
  }
  for (const [a, b] of dependencyEdges(issues)) {
    if (!parent.has(a)) parent.set(a, a)
    if (!parent.has(b)) parent.set(b, b)
    parent.set(find(a), find(b))
  }
  const groups = new Map<number, number[]>()
  for (const id of parent.keys()) {
    const root = find(id)
    groups.set(root, [...(groups.get(root) ?? []), id])
  }
  return [...groups.values()]
}

/** 要画进图里的 issue：有依赖关系的；scope 为 open 时去掉整组都已完成/取消的 */
export function graphIssues(issues: Issue[], scope: GraphScope): Issue[] {
  const byId = new Map(issues.map((i) => [i.id, i]))
  const keep = new Set(
    dependencyGroups(issues)
      .filter((g) => scope === 'all' || g.some((id) => !isFinished(byId.get(id)!.status)))
      .flat(),
  )
  return issues.filter((i) => keep.has(i.id))
}

/** 用 dagre 分层布局：从左到右，同一层上下排开 */
export function layoutIssueGraph(issues: Issue[]): GraphLayout {
  const g = new Graph()
  g.setGraph({ rankdir: 'LR', nodesep: 20, ranksep: 64, marginx: 16, marginy: 16 })
  g.setDefaultEdgeLabel(() => ({}))
  // 按编号加入：布局稳定，同层里旧的在上
  const sorted = [...issues].sort((a, b) => a.id - b.id)
  for (const i of sorted) g.setNode(String(i.id), { width: NODE_WIDTH, height: NODE_HEIGHT })
  const edges = dependencyEdges(sorted)
  for (const [from, to] of edges) g.setEdge(String(from), String(to))
  layout(g)
  const status = new Map(issues.map((i) => [i.id, i.status]))
  const label = g.graph()
  return {
    nodes: sorted.map((issue) => {
      const n = g.node(String(issue.id))
      return { issue, x: n.x! - NODE_WIDTH / 2, y: n.y! - NODE_HEIGHT / 2 }
    }),
    edges: edges.map(([from, to]) => ({
      from,
      to,
      settled: isFinished(status.get(from)!),
      points: g.edge(String(from), String(to)).points!,
    })),
    width: label.width!,
    height: label.height!,
  }
}

/** 边的 SVG 路径：依次连接 dagre 给的折点 */
export function edgePath(points: Point[]): string {
  return points.map((p, k) => `${k === 0 ? 'M' : 'L'}${p.x.toFixed(1)},${p.y.toFixed(1)}`).join(' ')
}

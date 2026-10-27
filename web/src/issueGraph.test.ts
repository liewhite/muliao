import { describe, expect, it } from 'vitest'
import type { Issue } from './api'
import { NODE_WIDTH, dependencyGroups, edgePath, graphIssues, layoutIssueGraph } from './issueGraph'

const issue = (id: number, over: Partial<Issue> = {}): Issue => ({
  id,
  title: `t${id}`,
  description: '',
  status: 'todo',
  priority: 'none',
  owner: null,
  assignee: null,
  creator: 'user',
  sessions: {},
  assignee_state: null,
  stalled: false,
  depends_on: [],
  blocks: [],
  waiting_on: [],
  finished: false,
  reviewable: false,
  created_at: '2026-10-21T00:00:00Z',
  updated_at: '2026-10-21T00:00:00Z',
  ...over,
})

describe('issue graph', () => {
  // 1 ← 2 ← 3（3 依赖 2，2 依赖 1）；4 ← 5 全部完成；6 孤立；7 依赖未加载的 99
  const issues = [
    issue(1, { status: 'done', finished: true }),
    issue(2, { depends_on: [1] }),
    issue(3, { depends_on: [2, 1], waiting_on: [2] }),
    issue(4, { status: 'done', finished: true }),
    issue(5, { status: 'cancelled', finished: true, depends_on: [4] }),
    issue(6),
    issue(7, { depends_on: [99] }),
  ]

  it('groups issues connected by dependencies, leaving out unrelated ones', () => {
    const groups = dependencyGroups(issues).map((g) => [...g].sort())
    expect(groups.sort()).toEqual([
      [1, 2, 3],
      [4, 5],
    ])
  })

  it('drops groups that are entirely finished unless showing all', () => {
    expect(graphIssues(issues, 'open').map((i) => i.id)).toEqual([1, 2, 3])
    expect(graphIssues(issues, 'all').map((i) => i.id)).toEqual([1, 2, 3, 4, 5])
  })

  it('lays dependencies out left of the issues that depend on them', () => {
    const g = layoutIssueGraph(graphIssues(issues, 'open'))
    const x = new Map(g.nodes.map((n) => [n.issue.id, n.x]))
    expect(x.get(1)!).toBeLessThan(x.get(2)!)
    expect(x.get(2)!).toBeLessThan(x.get(3)!)
    expect(g.width).toBeGreaterThanOrEqual(3 * NODE_WIDTH)
    expect(g.edges.map((e) => [e.from, e.to, e.settled])).toEqual([
      [1, 2, true],
      [2, 3, false],
      [1, 3, true],
    ])
    expect(g.edges.every((e) => e.points.length >= 2)).toBe(true)
  })

  it('builds an svg path through the points', () => {
    expect(edgePath([{ x: 0, y: 1 }, { x: 2.25, y: 3 }])).toBe('M0.0,1.0 L2.3,3.0')
  })
})

import { describe, expect, it } from 'vitest'
import type { Agent, Issue, IssueActivity } from './api'
import { FOLD_LIMIT, activityText, assigneeLabel, foldIssues, groupByStatus, moveIssues, parseIssueRefs } from './issues'

const issue = (id: number, over: Partial<Issue> = {}): Issue => ({
  id,
  title: `t${id}`,
  description: '',
  status: 'todo',
  priority: 'none',
  owner: null,
  assignee: null,
  creator_agent_id: null,
  sessions: {},
  assignee_session: null,
  stalled: false,
  depends_on: [],
  blocks: [],
  waiting_on: [],
  created_at: '2026-10-21T00:00:00Z',
  updated_at: '2026-10-21T00:00:00Z',
  ...over,
})
const agents = [{ id: 'a1', name: 'alpha' }] as unknown as Agent[]

describe('issues', () => {
  it('folds a column to the first FOLD_LIMIT issues unless expanded', () => {
    const many = Array.from({ length: FOLD_LIMIT + 3 }, (_, k) => issue(k + 1))
    expect(foldIssues(many, false).map((i) => i.id)).toEqual(many.slice(0, FOLD_LIMIT).map((i) => i.id))
    expect(foldIssues(many, true)).toHaveLength(FOLD_LIMIT + 3)
    const few = many.slice(0, 2)
    expect(foldIssues(few, false)).toEqual(few)
  })

  it('groups by status, most recently updated first', () => {
    const groups = groupByStatus([
      issue(1, { updated_at: '2026-10-21T01:00:00Z' }),
      issue(2, { updated_at: '2026-10-21T02:00:00Z' }),
      issue(3, { priority: 'urgent' }),
      issue(4, { status: 'done' }),
    ])
    // 只按最近更新排，不看优先级
    expect(groups.todo.map((i) => i.id)).toEqual([2, 1, 3])
    expect(groups.done.map((i) => i.id)).toEqual([4])
    expect(groups.backlog).toEqual([])
  })

  it('moves every pending issue to its new status', () => {
    const moves = new Map([
      [2, { status: 'done' as const, at: '2026-10-21T09:00:00Z' }],
      [3, { status: 'blocked' as const, at: '2026-10-21T09:00:01Z' }],
    ])
    const moved = moveIssues([issue(1), issue(2), issue(3)], moves)
    expect(moved.map((i) => i.status)).toEqual(['todo', 'done', 'blocked'])
    expect(moved[1].updated_at).toBe('2026-10-21T09:00:00Z')
    expect(moved[2].updated_at).toBe('2026-10-21T09:00:01Z')
  })

  it('labels assignees', () => {
    expect(assigneeLabel(agents, null)).toBe('未指派')
    expect(assigneeLabel(agents, 'user')).toBe('我')
    expect(assigneeLabel(agents, 'a1')).toBe('alpha')
  })

  it('describes changes in the timeline', () => {
    const change = (kind: IssueActivity['kind'], from: unknown, to: unknown): IssueActivity => ({
      id: 1,
      created_at: '',
      actor_agent_id: null,
      kind,
      body: null,
      change: { from, to },
    })
    expect(activityText(agents, change('status', 'todo', 'done'))).toBe('把状态从「Todo」改为「完成」')
    expect(activityText(agents, change('assignee', null, 'a1'))).toBe('把指派从「未指派」改为「alpha」')
    expect(activityText(agents, change('depends_on', [1, 2], []))).toBe('把依赖从「#1, #2」改为「无」')
    expect(activityText(agents, { ...change('dependency', 'todo', 'done'), change: { issue: 3, from: 'todo', to: 'done' } })).toBe(
      '把依赖 #3 的状态从「Todo」改为「完成」',
    )
  })
})

describe('parseIssueRefs', () => {
  it('reads numbers with or without #, separated by commas or spaces', () => {
    expect(parseIssueRefs('#1, 2 ，#30')).toEqual([1, 2, 30])
    expect(parseIssueRefs('  ')).toEqual([])
    expect(parseIssueRefs('#1, abc')).toBeNull()
  })
})

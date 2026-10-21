import { describe, expect, it } from 'vitest'
import type { Agent, Issue, IssueActivity } from './api'
import { FOLD_LIMIT, activityText, assigneeLabel, foldIssues, groupByStatus, moveIssue, stalled } from './issues'

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

  it('moves an issue to another status', () => {
    const moved = moveIssue([issue(1), issue(2)], 2, 'done', '2026-10-21T09:00:00Z')
    expect(moved.map((i) => i.status)).toEqual(['todo', 'done'])
    expect(moved[1].updated_at).toBe('2026-10-21T09:00:00Z')
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
  })
})

describe('stalled', () => {
  const base = { id: 1, owner: null, title: '', description: '', priority: 'none', creator_agent_id: null, sessions: {}, created_at: '', updated_at: '' } as const
  it('flags an active agent issue that nobody is working on', () => {
    expect(stalled({ ...base, status: 'todo', assignee: 'a1', assignee_session: 'idle' })).toBe(true)
    expect(stalled({ ...base, status: 'todo', assignee: 'a1', assignee_session: null })).toBe(true)
    expect(stalled({ ...base, status: 'in_progress', assignee: 'a1', assignee_session: 'running' })).toBe(false)
    expect(stalled({ ...base, status: 'backlog', assignee: 'a1', assignee_session: 'idle' })).toBe(false)
    expect(stalled({ ...base, status: 'todo', assignee: 'user', assignee_session: null })).toBe(false)
  })
})

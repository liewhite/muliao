import { describe, expect, it } from 'vitest'
import type { Agent, Issue, IssueActivity } from './api'
import { activityText, assigneeLabel, groupByStatus, moveIssue } from './issues'

const issue = (id: number, over: Partial<Issue> = {}): Issue => ({
  id,
  title: `t${id}`,
  description: '',
  status: 'todo',
  priority: 'none',
  assignee: null,
  creator_agent_id: null,
  sessions: {},
  created_at: '2026-10-21T00:00:00Z',
  updated_at: '2026-10-21T00:00:00Z',
  ...over,
})
const agents = [{ id: 'a1', name: 'alpha' }] as unknown as Agent[]

describe('issues', () => {
  it('groups by status, urgent first then most recently updated', () => {
    const groups = groupByStatus([
      issue(1, { updated_at: '2026-10-21T01:00:00Z' }),
      issue(2, { updated_at: '2026-10-21T02:00:00Z' }),
      issue(3, { priority: 'urgent' }),
      issue(4, { status: 'done' }),
    ])
    expect(groups.todo.map((i) => i.id)).toEqual([3, 2, 1])
    expect(groups.done.map((i) => i.id)).toEqual([4])
    expect(groups.backlog).toEqual([])
  })

  it('moves an issue to another status', () => {
    expect(moveIssue([issue(1), issue(2)], 2, 'done').map((i) => i.status)).toEqual(['todo', 'done'])
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

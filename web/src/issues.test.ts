import { describe, expect, it } from 'vitest'
import type { Agent, Issue, IssueActivity } from './api'
import { FOLD_LIMIT, activityText, assigneeLabel, foldIssues, groupByStatus, issueIdOf, moveIssues, ownerHue, parseIssueRefs } from './issues'

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
      issue(4, { status: 'done', finished: true }),
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
      actor: 'user',
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
    expect(activityText(agents, { ...change('run_failed', null, null), change: null, body: 'Boom: bad' })).toBe(
      '在 issue 会话里的运行失败了：Boom: bad',
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

describe('ownerHue', () => {
  const agent = (id: string, created_at: string) => ({ id, name: id, created_at }) as unknown as Agent
  const team = [agent('b', '2026-10-02T00:00:00Z'), agent('a', '2026-10-01T00:00:00Z'), agent('c', '2026-10-03T00:00:00Z')]

  it('gives agents hues a golden angle apart in creation order, whatever the list order', () => {
    expect(['a', 'b', 'c'].map((id) => ownerHue(team, id))).toEqual([0, 138, 275])
    expect(ownerHue([...team].reverse(), 'b')).toBe(138)
  })

  it('keeps existing hues when a newer agent is added', () => {
    const grown = [...team, agent('0', '2026-10-04T00:00:00Z')]
    expect(['a', 'b', 'c'].map((id) => ownerHue(grown, id))).toEqual([0, 138, 275])
  })

  it('is null without an owner, for a deleted agent, or before agents load', () => {
    expect(ownerHue(team, null)).toBeNull()
    expect(ownerHue(team, 'gone')).toBeNull()
    expect(ownerHue(undefined, 'a')).toBeNull()
  })
})

describe('issueIdOf', () => {
  it('issue 会话取 origin_ref 里的编号', () => {
    expect(issueIdOf({ id: 's', origin: 'issue', origin_ref: '120' })).toBe(120)
  })

  it('不是 issue 会话或编号不合法时报错', () => {
    expect(() => issueIdOf({ id: 's', origin: 'user', origin_ref: null })).toThrow(/会话 s/)
    expect(() => issueIdOf({ id: 's', origin: 'issue', origin_ref: null })).toThrow()
    expect(() => issueIdOf({ id: 's', origin: 'issue', origin_ref: 'abc' })).toThrow()
    expect(() => issueIdOf({ id: 's', origin: 'issue', origin_ref: '0' })).toThrow()
  })
})

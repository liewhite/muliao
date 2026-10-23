import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import { shouldRefetch, type TaskKind } from './events'
import { Loading, type Loaded } from './ui'

const state = (data: string | undefined, error: string | null): Loaded<string> => ({
  data,
  error,
  loading: false,
  reload: async () => {},
})
const html = (s: Loaded<string>) => renderToStaticMarkup(<Loading state={s}>{(d) => <p>content:{d}</p>}</Loading>)

describe('Loading', () => {
  it('keeps the loaded content and shows the error when a reload fails', () => {
    const out = html(state('old', 'network down'))
    expect(out).toContain('content:old')
    expect(out).toContain('network down')
  })

  it('replaces the page with the error when the first load fails', () => {
    const out = html(state(undefined, 'network down'))
    expect(out).not.toContain('content:')
    expect(out).toContain('加载失败')
  })

  it('renders the content without an error', () => {
    const out = html(state('new', null))
    expect(out).toContain('content:new')
    expect(out).not.toContain('失败')
  })
})

describe('shouldRefetch', () => {
  const task = (kind: TaskKind) =>
    ({ type: 'task', task_id: 1, kind, status: 'done', session_id: 's', agent: 'a', error: null }) as const

  it('refetches after connecting or a run status change, not on deliveries', () => {
    expect(shouldRefetch({ type: 'connected' }, 'sessions')).toBe(true)
    expect(shouldRefetch(task('run'), 'sessions')).toBe(true)
    expect(shouldRefetch(task('deliver'), 'sessions')).toBe(false)
  })

  it('refetches after this page changed the same resource only', () => {
    expect(shouldRefetch({ type: 'changed', resource: 'sessions' }, 'sessions')).toBe(true)
    expect(shouldRefetch({ type: 'changed', resource: 'agents' }, 'sessions')).toBe(false)
  })

  it('refetches sessions when a notification is written: their unread mark comes from notifications', () => {
    const note = { type: 'notification', notification_id: 1, session_id: 's' } as const
    expect(shouldRefetch(note, 'sessions')).toBe(true)
    expect(shouldRefetch(note, 'agents')).toBe(false)
    // 系统通知（前置检查告警投不到 agent 时的兜底、推送测试）不指向会话
    expect(shouldRefetch({ ...note, session_id: null }, 'sessions')).toBe(false)
  })
})

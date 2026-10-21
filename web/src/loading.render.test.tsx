import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import { shouldRefetch } from './events'
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
  const task = (kind: 'run' | 'deliver') =>
    ({ type: 'task', task_id: 1, kind, status: 'done', session_id: 's', agent: 'a', error: null }) as const

  it('refetches after a reconnect or a run status change, not on deliveries', () => {
    expect(shouldRefetch({ type: 'reconnected' })).toBe(true)
    expect(shouldRefetch(task('run'))).toBe(true)
    expect(shouldRefetch(task('deliver'))).toBe(false)
  })
})

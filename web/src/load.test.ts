import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { PageEvent } from './events'

// 服务端事件换成手动发：Loader 经 listenServerEvents 订阅
const listeners = new Set<(e: PageEvent) => void>()
vi.mock('./events', async (original) => ({
  ...(await original<typeof import('./events')>()),
  listenServerEvents: (l: (e: PageEvent) => void) => {
    listeners.add(l)
    return () => listeners.delete(l)
  },
}))
const { Loader, byReads, refreshShown } = await import('./load')
const { api } = await import('./api')

const emit = (e: PageEvent) => listeners.forEach((l) => l(e))
const settle = () => new Promise((r) => setTimeout(r, 0))

// 每个接口请求了几次（按路径）；都回空列表或空对象
let requests: string[] = []
beforeEach(() => {
  requests = []
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string) => {
      requests.push(url.split('?')[0])
      return new Response('[]', { status: 200, headers: { 'Content-Type': 'application/json' } })
    }),
  )
})
afterEach(() => {
  listeners.clear()
  vi.unstubAllGlobals()
})
const count = (path: string) => requests.filter((r) => r === path).length

describe('byReads', () => {
  it('refetches after connecting or when a resource it read changed, whoever changed it', () => {
    const reads = new Set(['sessions'] as const)
    expect(byReads({ type: 'connected' }, reads)).toBe(true)
    expect(byReads({ type: 'changed', resource: 'sessions' }, reads)).toBe(true)
    expect(byReads({ type: 'changed', resource: 'agents' }, reads)).toBe(false)
  })

  it('does not derive anything itself: task changes come as changed tasks from the server', () => {
    const task = { type: 'task', task_id: 1, kind: 'run', status: 'done', session_id: 's', agent: 'a', error: null } as const
    expect(byReads(task, new Set(['tasks']))).toBe(false)
  })
})

describe('Loader', () => {
  it('subscribes to what the endpoints it requested declare, not a hand-written category', async () => {
    // /api/issues 声明读 issues、sessions、tasks（后端 x-reads）
    const loader = new Loader(() => api.issues())
    const stop = loader.subscribe(() => {})
    await settle()
    expect(count('/api/issues')).toBe(1)
    emit({ type: 'changed', resource: 'agents' })
    await settle()
    expect(count('/api/issues')).toBe(1)
    emit({ type: 'changed', resource: 'sessions' })
    await settle()
    expect(count('/api/issues')).toBe(2)
    stop()
    expect(listeners.size).toBe(0)
  })

  it('collects the reads of every request, also those after an await', async () => {
    const loader = new Loader(async () => {
      await api.tasks()
      return api.scriptGrants()
    })
    const stop = loader.subscribe(() => {})
    await settle()
    emit({ type: 'changed', resource: 'workspace' })
    await settle()
    expect(count('/api/script-grants')).toBe(2)
    stop()
  })

  it('does not refetch data that does not change with the server', async () => {
    const loader = new Loader(() => api.features())
    const stop = loader.subscribe(() => {})
    await settle()
    emit({ type: 'changed', resource: 'agents' })
    emit({ type: 'connected' })
    await settle()
    expect(count('/api/features')).toBe(1)
    expect(loader.live()).toBe(false)
    stop()
  })

  it('refetches on its own events when given them, instead of by reads', async () => {
    const loader = new Loader(() => api.issues(), (e) => e.type === 'saved' && e.session_id === 's1')
    const stop = loader.subscribe(() => {})
    await settle()
    emit({ type: 'changed', resource: 'issues' })
    emit({ type: 'saved', session_id: 's2' })
    await settle()
    expect(count('/api/issues')).toBe(1)
    emit({ type: 'saved', session_id: 's1' })
    await settle()
    expect(count('/api/issues')).toBe(2)
    stop()
  })

  it('retries after connecting even when its first load failed', async () => {
    let fail = true
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string) => {
        requests.push(url)
        if (fail) throw new TypeError('network down')
        return new Response('[]', { status: 200, headers: { 'Content-Type': 'application/json' } })
      }),
    )
    const loader = new Loader(() => api.tasks())
    const stop = loader.subscribe(() => {})
    await settle()
    expect(loader.snapshot().error).toBe('network down')
    fail = false
    emit({ type: 'connected' })
    await settle()
    expect(loader.snapshot()).toEqual({ data: [], error: null, loading: false })
    stop()
  })

  it('keeps only the latest request: a late older response cannot overwrite a newer one', async () => {
    const pending: ((v: string) => void)[] = []
    const loader = new Loader(() => new Promise<string>((r) => pending.push(r)))
    const first = loader.reload()
    const second = loader.reload()
    pending[1]('new')
    await second
    pending[0]('old')
    await first
    expect(loader.snapshot().data).toBe('new')
  })

  it('an overtaken reload returns only once the latest one has landed', async () => {
    const pending: ((v: string) => void)[] = []
    const loader = new Loader(() => new Promise<string>((r) => pending.push(r)))
    let firstDone = false
    const first = loader.reload().then(() => (firstDone = true))
    void loader.reload()
    pending[0]('old')
    await settle()
    expect(firstDone).toBe(false)
    pending[1]('new')
    await first
    expect(loader.snapshot().data).toBe('new')
  })

  it('keeps the data and records the error when a reload fails', async () => {
    let fail = false
    const loader = new Loader(async () => {
      if (fail) throw new Error('network down')
      return 'ok'
    })
    await loader.reload()
    fail = true
    await loader.reload()
    expect(loader.snapshot()).toEqual({ data: 'ok', error: 'network down', loading: false })
  })

  it('refreshes what is shown after a user action, not hidden or static data', async () => {
    const shown = new Loader(() => api.issues())
    const hidden = new Loader(() => api.tasks())
    const static_ = new Loader(() => api.features())
    const stopShown = shown.subscribe(() => {})
    const stopStatic = static_.subscribe(() => {})
    hidden.subscribe(() => {})()
    await settle()
    await refreshShown()
    expect([count('/api/issues'), count('/api/tasks'), count('/api/features')]).toEqual([2, 1, 1])
    stopShown()
    stopStatic()
  })
})

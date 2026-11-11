import { afterEach, describe, expect, it, vi } from 'vitest'
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
const { Loader, refetches, refreshShown } = await import('./load')

const emit = (e: PageEvent) => listeners.forEach((l) => l(e))
const settle = () => new Promise((r) => setTimeout(r, 0))

afterEach(() => listeners.clear())

describe('refetches', () => {
  it('refetches after connecting or when the server says the same resource changed, whoever changed it', () => {
    expect(refetches({ type: 'connected' }, 'sessions')).toBe(true)
    expect(refetches({ type: 'changed', resource: 'sessions' }, 'sessions')).toBe(true)
    expect(refetches({ type: 'changed', resource: 'agents' }, 'sessions')).toBe(false)
  })

  it('does not derive anything itself: task status changes come as changed from the server', () => {
    const task = {
      type: 'task',
      task_id: 1,
      kind: 'run',
      status: 'done',
      session_id: 's',
      agent: 'a',
      error: null,
    } as const
    expect(refetches(task, 'sessions')).toBe(false)
  })
})

describe('Loader', () => {
  it('loads on the first subscriber and refetches when its resource changes', async () => {
    let n = 0
    const loader = new Loader('issues', async () => ++n)
    const stop = loader.subscribe(() => {})
    await settle()
    expect(loader.snapshot().data).toBe(1)
    emit({ type: 'changed', resource: 'agents' })
    await settle()
    expect(loader.snapshot().data).toBe(1)
    emit({ type: 'changed', resource: 'issues' })
    await settle()
    expect(loader.snapshot().data).toBe(2)
    stop()
    expect(listeners.size).toBe(0)
  })

  it('does not listen at all without a resource', async () => {
    const loader = new Loader(null, async () => 1)
    const stop = loader.subscribe(() => {})
    expect(listeners.size).toBe(0)
    stop()
  })

  it('keeps only the latest request: a late older response cannot overwrite a newer one', async () => {
    const pending: ((v: string) => void)[] = []
    const loader = new Loader(null, () => new Promise<string>((r) => pending.push(r)))
    const first = loader.reload()
    const second = loader.reload()
    pending[1]('new')
    await second
    pending[0]('old')
    await first
    expect(loader.snapshot().data).toBe('new')
  })

  it('keeps the data and records the error when a reload fails', async () => {
    let fail = false
    const loader = new Loader(null, async () => {
      if (fail) throw new Error('network down')
      return 'ok'
    })
    await loader.reload()
    fail = true
    await loader.reload()
    expect(loader.snapshot()).toEqual({ data: 'ok', error: 'network down', loading: false })
  })

  it('refreshes what is shown after a user action, not hidden or self-refreshing data', async () => {
    let shownLoads = 0
    let hiddenLoads = 0
    let ownLoads = 0
    const shown = new Loader('issues', async () => ++shownLoads)
    const hidden = new Loader('issues', async () => ++hiddenLoads)
    const own = new Loader(null, async () => ++ownLoads)
    const stopShown = shown.subscribe(() => {})
    const stopOwn = own.subscribe(() => {})
    hidden.subscribe(() => {})()
    await settle()
    await refreshShown()
    expect([shownLoads, hiddenLoads, ownLoads]).toEqual([2, 1, 1])
    expect(shown.snapshot().data).toBe(2)
    stopShown()
    stopOwn()
  })
})

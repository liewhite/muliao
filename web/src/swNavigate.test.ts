import { afterEach, describe, expect, it, vi } from 'vitest'

class FakeContainer extends EventTarget {
  started = false
  startMessages() {
    this.started = true
  }
  send(data: unknown) {
    this.dispatchEvent(new MessageEvent('message', { data }))
  }
}

async function setup() {
  const container = new FakeContainer()
  vi.stubGlobal('navigator', { serviceWorker: container })
  vi.resetModules()
  const mod = await import('./swNavigate')
  mod.listenForNavigation()
  return { container, ...mod }
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('swNavigate', () => {
  it('外壳挂载前到的跳转请求暂存，挂载时补上（只留最近一次）', async () => {
    const { container, handleNavigation } = await setup()
    expect(container.started).toBe(true)
    container.send({ type: 'navigate', url: '/issues/1' })
    container.send({ type: 'navigate', url: '/issues/2' })
    const seen: string[] = []
    handleNavigation((url) => seen.push(url))
    expect(seen).toEqual(['/issues/2'])
  })

  it('挂载后直接跳转，不认识的消息不理；取消后不再收到', async () => {
    const { container, handleNavigation } = await setup()
    const seen: string[] = []
    const stop = handleNavigation((url) => seen.push(url))
    container.send({ type: 'pushed' })
    container.send({ type: 'navigate', url: '/sessions/abc' })
    expect(seen).toEqual(['/sessions/abc'])
    stop()
    container.send({ type: 'navigate', url: '/issues/3' })
    expect(seen).toEqual(['/sessions/abc'])
    // 取消期间到的留给下一次挂载
    handleNavigation((url) => seen.push(url))
    expect(seen).toEqual(['/sessions/abc', '/issues/3'])
  })
})

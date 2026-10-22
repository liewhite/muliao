import { afterEach, describe, expect, it, vi } from 'vitest'
import swSource from '../public/sw.js?raw'

// 测试用的同源存储与广播：页面与 Service Worker 共用同一份（与浏览器里同源共享一致）
class FakeCache {
  entries = new Map<string, Response>()
  async put(key: string, resp: Response) {
    this.entries.set(key, resp)
  }
  async match(key: string) {
    return this.entries.get(key)?.clone()
  }
  async delete(key: string) {
    return this.entries.delete(key)
  }
  async keys() {
    return [...this.entries.keys()]
  }
}

class FakeOrigin {
  stores = new Map<string, FakeCache>()
  // 页面被挂起：广播送不到
  suspended = false
  channels = new Map<EventTarget, string>()
  caches = {
    open: async (name: string) => {
      let cache = this.stores.get(name)
      if (cache === undefined) {
        cache = new FakeCache()
        this.stores.set(name, cache)
      }
      return cache
    },
  }
  BroadcastChannel = broadcastChannelOf(this)
}

function broadcastChannelOf(origin: FakeOrigin) {
  return class extends EventTarget {
    name: string
    constructor(name: string) {
      super()
      this.name = name
      origin.channels.set(this, name)
    }
    postMessage(data: unknown) {
      if (origin.suspended) return
      for (const [target, name] of origin.channels) {
        if (name === this.name && target !== this) {
          queueMicrotask(() => target.dispatchEvent(new MessageEvent('message', { data })))
        }
      }
    }
    close() {
      origin.channels.delete(this)
    }
  }
}

type FakeClient = { focused: boolean; visibilityState: string; focus: () => Promise<void> }

function fakeClient(over: Partial<FakeClient> = {}): FakeClient {
  return { focused: false, visibilityState: 'hidden', focus: async () => {}, ...over }
}

/** 在假的 ServiceWorkerGlobalScope 里执行 public/sw.js，返回点通知的入口；windows 是 clients.matchAll 列出的窗口 */
function loadServiceWorker(origin: FakeOrigin, windows: FakeClient[]) {
  const listeners = new Map<string, (e: unknown) => void>()
  const opened: string[] = []
  const self = {
    caches: origin.caches,
    BroadcastChannel: origin.BroadcastChannel,
    navigator: {},
    registration: {},
    addEventListener: (type: string, fn: (e: unknown) => void) => listeners.set(type, fn),
    clients: {
      matchAll: async () => windows,
      openWindow: async (url: string) => {
        opened.push(url)
      },
      claim: async () => {},
    },
  }
  new Function('self', swSource)(self)
  async function click(url: string, action = '') {
    let done: Promise<unknown> = Promise.resolve()
    listeners.get('notificationclick')!({
      action,
      notification: { close: () => {}, data: { url, issueId: null } },
      waitUntil: (p: Promise<unknown>) => (done = p),
    })
    await done
  }
  return { click, opened }
}

/** 页面一侧：假的 navigator / document / window / location，加载 swNavigate 并开始监听 */
async function loadPage(origin: FakeOrigin, path = '/') {
  const doc = Object.assign(new EventTarget(), { visibilityState: 'visible' })
  const win = new EventTarget()
  vi.stubGlobal('navigator', { serviceWorker: {} })
  vi.stubGlobal('caches', origin.caches)
  vi.stubGlobal('BroadcastChannel', origin.BroadcastChannel)
  vi.stubGlobal('document', doc)
  vi.stubGlobal('window', win)
  vi.stubGlobal('location', { pathname: path, search: '' })
  vi.resetModules()
  const mod = await import('./swNavigate')
  mod.listenForNavigation()
  return { doc, win, ...mod }
}

const settle = () => new Promise((r) => setTimeout(r, 0))

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('点通知跳转（sw.js + swNavigate）', () => {
  it('应用在前台：SW 留下 url 并广播，页面马上取走跳转，取走即删', async () => {
    const origin = new FakeOrigin()
    const page = await loadPage(origin)
    const seen: string[] = []
    page.handleNavigation((url) => seen.push(url))
    const sw = loadServiceWorker(origin, [fakeClient({ focused: true, visibilityState: 'visible' })])
    await sw.click('/issues/7')
    await settle()
    expect(seen).toEqual(['/issues/7'])
    expect(sw.opened).toEqual([])
    expect(await page.takePending()).toBeNull()
  })

  it('应用开着但 SW 列不出它的窗口（iOS）：openWindow 不跳也照样靠广播跳过去', async () => {
    const origin = new FakeOrigin()
    const page = await loadPage(origin, '/sessions/old')
    const seen: string[] = []
    page.handleNavigation((url) => seen.push(url))
    const sw = loadServiceWorker(origin, [])
    await sw.click('/issues/8')
    await settle()
    expect(sw.opened).toEqual(['/issues/8'])
    expect(seen).toEqual(['/issues/8'])
  })

  it('应用在后台、广播丢了：回到前台或获得焦点时取到 url 跳转', async () => {
    const origin = new FakeOrigin()
    const page = await loadPage(origin)
    const seen: string[] = []
    page.handleNavigation((url) => seen.push(url))
    origin.suspended = true
    page.doc.visibilityState = 'hidden'
    const sw = loadServiceWorker(origin, [fakeClient({ focus: async () => Promise.reject(new Error('denied')) })])
    await sw.click('/sessions/abc')
    page.win.dispatchEvent(new Event('focus'))
    await settle()
    expect(seen).toEqual([])
    origin.suspended = false
    page.doc.visibilityState = 'visible'
    page.doc.dispatchEvent(new Event('visibilitychange'))
    await settle()
    expect(seen).toEqual(['/sessions/abc'])
    origin.suspended = true
    await sw.click('/issues/4')
    page.win.dispatchEvent(new Event('focus'))
    await settle()
    expect(seen).toEqual(['/sessions/abc', '/issues/4'])
  })

  it('回到前台时广播、visibilitychange、focus 同时来取，只跳一次；连点两条只认最新的', async () => {
    const origin = new FakeOrigin()
    const page = await loadPage(origin)
    const seen: string[] = []
    page.handleNavigation((url) => seen.push(url))
    origin.suspended = true
    const sw = loadServiceWorker(origin, [fakeClient()])
    await sw.click('/issues/1')
    await new Promise((r) => setTimeout(r, 2))
    await sw.click('/issues/2')
    origin.suspended = false
    const wake = new origin.BroadcastChannel('muliao-pending-navigation')
    wake.postMessage({ type: 'navigate' })
    page.doc.dispatchEvent(new Event('visibilitychange'))
    page.win.dispatchEvent(new Event('focus'))
    await settle()
    expect(seen).toEqual(['/issues/2'])
    expect(origin.stores.get('muliao-pending-navigation')!.entries.size).toBe(0)
  })

  it('看不见的页（桌面后台标签）收到广播也不取，留给用户看着的页', async () => {
    const origin = new FakeOrigin()
    const page = await loadPage(origin)
    page.doc.visibilityState = 'hidden'
    const seen: string[] = []
    page.handleNavigation((url) => seen.push(url))
    await loadServiceWorker(origin, [fakeClient({ visibilityState: 'visible' })]).click('/issues/6')
    await settle()
    expect(seen).toEqual([])
    expect(await page.takePending()).toBe('/issues/6')
  })

  it('应用已被杀掉：openWindow 只拉起首页时，页面加载后取到 url，登录后的外壳挂载时补上', async () => {
    const origin = new FakeOrigin()
    const sw = loadServiceWorker(origin, [])
    await sw.click('/issues/9', 'reject')
    expect(sw.opened).toEqual(['/issues/9?review=reject'])
    const page = await loadPage(origin, '/')
    await settle()
    const seen: string[] = []
    page.handleNavigation((url) => seen.push(url))
    expect(seen).toEqual(['/issues/9?review=reject'])
  })

  it('openWindow 直接打开在目标页时不重复跳转', async () => {
    const origin = new FakeOrigin()
    const sw = loadServiceWorker(origin, [])
    await sw.click('/issues/9')
    const page = await loadPage(origin, '/issues/9')
    await settle()
    const seen: string[] = []
    page.handleNavigation((url) => seen.push(url))
    expect(seen).toEqual([])
  })

  it('过期的待跳转不再跳（那次没打开应用，过后自己打开）', async () => {
    const origin = new FakeOrigin()
    const { takePending, PENDING_MAX_AGE_MS } = await loadPage(origin)
    origin.suspended = true
    await loadServiceWorker(origin, []).click('/issues/2')
    expect(await takePending(Date.now() + PENDING_MAX_AGE_MS + 1)).toBeNull()
  })

  it('外壳挂载前到的只留最近一次；取消后到的留给下一次挂载', async () => {
    const origin = new FakeOrigin()
    const page = await loadPage(origin)
    const sw = loadServiceWorker(origin, [fakeClient()])
    await sw.click('/issues/1')
    await settle()
    await sw.click('/issues/2')
    await settle()
    const seen: string[] = []
    const stop = page.handleNavigation((url) => seen.push(url))
    expect(seen).toEqual(['/issues/2'])
    stop()
    await sw.click('/issues/3')
    await settle()
    expect(seen).toEqual(['/issues/2'])
    page.handleNavigation((url) => seen.push(url))
    expect(seen).toEqual(['/issues/2', '/issues/3'])
  })
})

// 点通知后的跳转。Service Worker（sw.js 的 open()）把要打开的 url 写进 Cache Storage，在 BroadcastChannel
// 上广播 {type: 'navigate'} 叫开着的页面来取，再聚焦窗口或（列不出窗口时）openWindow。页面在加载、回到前台、获得焦点、
// 收到广播时都去取，取走即删：iOS 主屏应用开着时 SW 可能列不出它的窗口（openWindow 对开着的应用不跳转），
// 挂起或冷启动时消息可能送不到，只靠消息会表现为点通知只打开应用、不跳到对应的 issue / 会话（#120、#133）。
// 取到的请求在登录后的外壳挂载前先存着，挂载时补上（只留最近一次）。

type Handler = (url: string) => void

// 与 sw.js 的 PENDING_CACHE 一致
const PENDING_CACHE = 'muliao-pending-navigation'
// 点通知之后这么久还没取走的不再跳（比如那次没打开应用，过后用户自己打开时不该被带走）
export const PENDING_MAX_AGE_MS = 2 * 60_000

let pending: string | null = null
let handler: Handler | null = null

function go(h: Handler, url: string) {
  // 应用被 openWindow 直接打开在这个页面时，不再重复压一条历史
  if (`${location.pathname}${location.search}` === url) return
  h(url)
}

function deliver(url: string) {
  if (handler) go(handler, url)
  else pending = url
}

type Entry = { key: Request; url: string; at: number }

/**
 * 取走 SW 留下的待跳转 url：每次点通知一条（各自的 key），取时全部删掉，只认最新的一条；几处同时来取时，
 * 只有删掉了自己看到的最新一条的那次算数（别人先删了它就不跳，不会拿着旧的一条再跳一次）。没有或已过期返回 null。
 */
export async function takePending(now: number = Date.now()): Promise<string | null> {
  const cache = await caches.open(PENDING_CACHE)
  const seen: Entry[] = []
  for (const key of await cache.keys()) {
    const resp = await cache.match(key)
    // 列出之后被别处取走了
    if (resp === undefined) continue
    seen.push({ key, ...((await resp.json()) as { url: string; at: number }) })
  }
  if (seen.length === 0) return null
  const newest = seen.reduce((a, b) => (b.at > a.at ? b : a))
  const deleted = await Promise.all(seen.map((e) => cache.delete(e.key)))
  if (!deleted[seen.indexOf(newest)]) return null
  return now - newest.at <= PENDING_MAX_AGE_MS ? newest.url : null
}

// 只有用户看得见的页面去取：桌面多标签时后台标签也收得到广播，不能让它抢走；被聚焦的页变可见时自己会取
function check() {
  if (document.visibilityState !== 'visible') return
  takePending().then(
    (url) => {
      if (url !== null) deliver(url)
    },
    (e) => console.error('reading the pending navigation failed', e),
  )
}

export function listenForNavigation(): void {
  // 没有 SW（非安全上下文）就没有推送，也没有 Cache Storage
  if (!navigator.serviceWorker) return
  new BroadcastChannel(PENDING_CACHE).addEventListener('message', (e: MessageEvent) => {
    if (e.data?.type === 'navigate') check()
  })
  document.addEventListener('visibilitychange', check)
  window.addEventListener('focus', check)
  check()
}

/** 由登录后的外壳调用：接收跳转请求，并补上挂载前到达的那一次；返回取消函数 */
export function handleNavigation(h: Handler): () => void {
  handler = h
  if (pending !== null) {
    const url = pending
    pending = null
    go(h, url)
  }
  return () => {
    if (handler === h) handler = null
  }
}

// 点通知时 Service Worker 发来的跳转请求（sw.js 的 open()：向已打开的窗口 postMessage {type: 'navigate', url}，再聚焦它）。
// 监听在应用一加载就挂上，不等登录确认：iOS 主屏应用被系统回收后，点通知会重新加载页面，消息在 api.me() 返回、
// 登录后的外壳挂载之前就到了；晚挂监听会丢掉它，表现为点通知只打开应用、不跳到对应的 issue / 会话。
// 外壳挂载前到的请求先存着，挂载时补上（只留最近一次）。

type Handler = (url: string) => void

let pending: string | null = null
let handler: Handler | null = null

export function listenForNavigation(): void {
  const container = navigator.serviceWorker
  if (!container) return
  container.addEventListener('message', (e: MessageEvent) => {
    if (e.data?.type !== 'navigate') return
    const url = e.data.url as string
    if (handler) handler(url)
    else pending = url
  })
  // 用 addEventListener 而不是 onmessage 时，消息要等 DOMContentLoaded 或 startMessages 才派发；显式开始，不依赖时机
  container.startMessages()
}

/** 由登录后的外壳调用：接收跳转请求，并补上挂载前到达的那一次；返回取消函数 */
export function handleNavigation(h: Handler): () => void {
  handler = h
  if (pending !== null) {
    const url = pending
    pending = null
    h(url)
  }
  return () => {
    if (handler === h) handler = null
  }
}

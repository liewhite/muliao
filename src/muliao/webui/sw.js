// Service Worker：只负责 Web Push 展示与点击跳转，不做离线缓存（数据以服务端为准）。

self.addEventListener('install', () => self.skipWaiting())
self.addEventListener('activate', (event) => event.waitUntil(self.clients.claim()))

self.addEventListener('push', (event) => {
  // 服务端契约：payload 为 {title, body, url, session_id, badge}，badge 是投递时的未读通知数
  const data = event.data.json()
  // iOS Safari 要求每个 push 都展示通知（userVisibleOnly），否则可能吊销订阅，所以不做「正在查看时静默」
  const shown = self.registration.showNotification(data.title, {
    body: data.body,
    icon: '/icon-192.png',
    badge: '/icon-192.png',
    tag: data.session_id || 'muliao',
    data: { url: data.url },
  })
  // 应用图标角标（Badging API，iOS 16.4+ 主屏应用支持；没有的浏览器只显示通知）
  const badged = 'setAppBadge' in self.navigator ? self.navigator.setAppBadge(data.badge) : Promise.resolve()
  event.waitUntil(
    (async () => {
      await Promise.all([shown, badged])
      // 角标里的数是投递那一刻的；已打开的窗口按服务端的当前数纠正（见 unread.ts）
      const windows = await self.clients.matchAll({ type: 'window' })
      windows.forEach((client) => client.postMessage({ type: 'pushed' }))
    })(),
  )
})

self.addEventListener('notificationclick', (event) => {
  event.notification.close()
  const url = event.notification.data.url
  event.waitUntil(
    (async () => {
      const windows = await self.clients.matchAll({ type: 'window', includeUncontrolled: true })
      for (const client of windows) {
        if ('focus' in client) {
          await client.focus()
          client.postMessage({ type: 'navigate', url })
          return
        }
      }
      await self.clients.openWindow(url)
    })(),
  )
})

// Service Worker：只负责 Web Push 展示与点击跳转，不做离线缓存（数据以服务端为准）。

self.addEventListener('install', () => self.skipWaiting())
self.addEventListener('activate', (event) => event.waitUntil(self.clients.claim()))

self.addEventListener('push', (event) => {
  // 服务端契约：payload 为 {title, body, url, session_id}
  const data = event.data.json()
  // iOS Safari 要求每个 push 都展示通知（userVisibleOnly），否则可能吊销订阅，所以不做「正在查看时静默」
  event.waitUntil(
    self.registration.showNotification(data.title, {
      body: data.body,
      icon: '/icon-192.png',
      badge: '/icon-192.png',
      tag: data.session_id || 'muliao',
      data: { url: data.url },
    }),
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

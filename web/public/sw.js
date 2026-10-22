// Service Worker：只负责 Web Push 展示与点击跳转，不做离线缓存（数据以服务端为准）。

self.addEventListener('install', () => self.skipWaiting())
self.addEventListener('activate', (event) => event.waitUntil(self.clients.claim()))

self.addEventListener('push', (event) => {
  // 服务端契约：payload 为 {title, body, url, session_id, issue_id, badge}，badge 是投递时的未读通知数；
  // issue_id 不为 null 是交给用户审核的 issue（url 是它的详情页，在那里通过或打回）
  const data = event.data.json()
  // iOS Safari 要求每个 push 都展示通知（userVisibleOnly），否则可能吊销订阅，所以不做「正在查看时静默」
  const shown = self.registration.showNotification(data.title, {
    body: data.body,
    icon: '/icon-192.png',
    badge: '/icon-192.png',
    // 同一会话的新通知替换旧的；待审核的 issue 各自一条，不被同一会话后来的通知替掉
    tag: data.issue_id !== null ? `issue-${data.issue_id}` : data.session_id || 'muliao',
    data: { url: data.url, issueId: data.issue_id },
    // 只有 Chromium 显示按钮（Safari、iOS 忽略，点开就是详情页的审核栏）
    actions:
      data.issue_id === null
        ? []
        : [
            { action: 'approve', title: '通过' },
            { action: 'reject', title: '打回' },
          ],
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

// 打开（或聚焦已打开的窗口并跳转到）url
async function open(url) {
  const windows = await self.clients.matchAll({ type: 'window', includeUncontrolled: true })
  // 优先用户正在看的窗口
  const client = windows.find((c) => c.focused) ?? windows.find((c) => c.visibilityState === 'visible') ?? windows[0]
  if (client === undefined) {
    await self.clients.openWindow(url)
    return
  }
  // 先发跳转再聚焦：focus 在部分平台（iOS 主屏应用）会被拒绝，拒绝了也要跳；页面在重新加载时消息由 swNavigate.ts 暂存
  client.postMessage({ type: 'navigate', url })
  try {
    await client.focus()
  } catch (e) {
    console.error('focusing the window failed', e)
  }
}

// 通知上的「通过」：直接调接口（同源请求带登录 cookie）；没成功（登录过期、已被改派……）就打开详情页让用户看到原因
async function approve(issueId, url) {
  let approved = false
  try {
    const resp = await fetch(`/api/issues/${issueId}/approve`, { method: 'POST', credentials: 'same-origin' })
    approved = resp.ok
  } catch (e) {
    console.error('approving from the notification failed', e)
  }
  if (!approved) {
    await open(url)
    return
  }
  // 在通知上处理了就是看过了：标已读，按服务端的未读数更新角标（失败只影响角标，下次打开应用时纠正）
  try {
    const read = await fetch(`/api/notifications/issues/${issueId}/read`, { method: 'POST', credentials: 'same-origin' })
    if (!read.ok) throw new Error(`HTTP ${read.status}`)
    const unread = await (await fetch('/api/notifications/unread', { credentials: 'same-origin' })).json()
    if ('setAppBadge' in self.navigator) await self.navigator.setAppBadge(unread.count)
  } catch (e) {
    console.error('marking the approved issue read failed', e)
  }
}

self.addEventListener('notificationclick', (event) => {
  event.notification.close()
  const { url, issueId } = event.notification.data
  if (event.action === 'approve') {
    event.waitUntil(approve(issueId, url))
    return
  }
  event.waitUntil(open(event.action === 'reject' ? `${url}?review=reject` : url))
})

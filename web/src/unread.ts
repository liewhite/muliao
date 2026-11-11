// 通知的未读数：导航上的数字与应用图标角标共用这一份，事实在服务端（GET /api/notifications/unread）。
// 推送到达时 Service Worker 按推送里带的未读数设置角标；应用打开时由这里按服务端的数刷新。

import { useEffect } from 'react'
import { api } from './api'
import { Loader, useLoader } from './load'

// 通知变了（谁标的已读、写下新通知都一样）就重新拉取，见 load.Loader
const unread = new Loader(async () => (await api.unreadNotifications()).count)

function setBadge(count: number) {
  // Badging API 不是所有浏览器都有（如 Firefox）；没有就只显示导航上的数字
  if (!('setAppBadge' in navigator)) return
  void (count > 0 ? navigator.setAppBadge(count) : navigator.clearAppBadge()).catch((e) =>
    console.error('app badge update failed', e),
  )
}

/**
 * 登录后在应用根部调用一次，返回未读数：通知变了时（Loader）、回到前台、收到推送时刷新，角标跟着它。
 * 刷新失败只记日志：后台同步，不打断用户的操作
 */
export function useUnreadCount(): number {
  const { data: count, error } = useLoader(unread)
  useEffect(() => {
    if (count !== undefined) setBadge(count)
  }, [count])
  useEffect(() => {
    if (error) console.error('unread count refresh failed', error)
  }, [error])
  useEffect(() => {
    // 推送里的未读数是投递那一刻的，可能晚于应用里的标记已读才到；Service Worker 设完角标后通知窗口按服务端的数纠正
    const onMessage = (e: MessageEvent) => {
      if (e.data?.type === 'pushed') void unread.reload()
    }
    navigator.serviceWorker?.addEventListener('message', onMessage)
    const onVisible = () => {
      if (document.visibilityState === 'visible') void unread.reload()
    }
    document.addEventListener('visibilitychange', onVisible)
    return () => {
      navigator.serviceWorker?.removeEventListener('message', onMessage)
      document.removeEventListener('visibilitychange', onVisible)
    }
  }, [])
  return count ?? 0
}

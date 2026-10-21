// 通知的未读数：导航上的数字与应用图标角标共用这一份，事实在服务端（GET /api/notifications/unread）。
// 推送到达时 Service Worker 按推送里带的未读数设置角标；应用打开时由这里按服务端的数刷新。

import { useEffect, useSyncExternalStore } from 'react'
import { api } from './api'
import { useServerEvents, type PageEvent } from './events'

let unread = 0
// 请求序号：并发刷新时只让最新一次的结果落下，晚到的旧响应不能覆盖新值
let latest = 0
const subscribers = new Set<() => void>()

function setBadge(count: number) {
  // Badging API 不是所有浏览器都有（如 Firefox）；没有就只显示导航上的数字
  if (!('setAppBadge' in navigator)) return
  void (count > 0 ? navigator.setAppBadge(count) : navigator.clearAppBadge()).catch((e) =>
    console.error('app badge update failed', e),
  )
}

/** 从服务端重新取未读数；标记已读之后调用 */
export async function refreshUnread(): Promise<void> {
  const id = ++latest
  const { count } = await api.unreadNotifications()
  if (id !== latest) return
  unread = count
  setBadge(count)
  subscribers.forEach((s) => s())
}

function subscribe(onChange: () => void) {
  subscribers.add(onChange)
  return () => {
    subscribers.delete(onChange)
  }
}

export function useUnreadCount(): number {
  return useSyncExternalStore(subscribe, () => unread)
}

/** 刷新未读数，失败只记日志：用于后台同步，不打断用户的操作 */
export const refreshUnreadLogged = () => void refreshUnread().catch((e) => console.error('unread count refresh failed', e))

/** 这个服务端事件之后通知可能变了：写下了新通知（不论有没有推送订阅）、一条领域事件处理完（如 SessionRead 排在
 * 更早的投递后面、由 worker 稍后标记通知已读），或断线期间可能错过了事件 */
export const notificationsMayHaveChanged = (e: PageEvent) =>
  e.type === 'reconnected' ||
  e.type === 'notification' ||
  (e.type === 'task' && e.kind === 'event' && e.status === 'done')

/** 登录后在应用根部调用一次：新通知投递、断线重连、回到前台、收到推送时刷新 */
export function useUnreadSync() {
  useServerEvents((e) => {
    if (notificationsMayHaveChanged(e)) refreshUnreadLogged()
  })
  useEffect(() => {
    refreshUnreadLogged()
    // 推送里的未读数是投递那一刻的，可能晚于应用里的标记已读才到；Service Worker 设完角标后通知窗口按服务端的数纠正
    const onMessage = (e: MessageEvent) => {
      if (e.data?.type === 'pushed') refreshUnreadLogged()
    }
    navigator.serviceWorker?.addEventListener('message', onMessage)
    const onVisible = () => {
      if (document.visibilityState === 'visible') refreshUnreadLogged()
    }
    document.addEventListener('visibilitychange', onVisible)
    return () => {
      navigator.serviceWorker?.removeEventListener('message', onMessage)
      document.removeEventListener('visibilitychange', onVisible)
    }
  }, [])
}

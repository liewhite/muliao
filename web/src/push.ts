import { api } from './api'

export type PushState = 'unsupported' | 'needs-install' | 'default' | 'denied' | 'granted'

function isIos(): boolean {
  return /iPad|iPhone|iPod/.test(navigator.userAgent)
}

function isStandalone(): boolean {
  return window.matchMedia('(display-mode: standalone)').matches || ('standalone' in navigator && navigator.standalone === true)
}

export function pushState(): PushState {
  if (!('serviceWorker' in navigator)) return 'unsupported'
  // iOS 只有「添加到主屏幕」后以独立模式打开时才提供 PushManager
  if (!('PushManager' in window)) return isIos() && !isStandalone() ? 'needs-install' : 'unsupported'
  return Notification.permission
}

export async function registerServiceWorker(): Promise<void> {
  if ('serviceWorker' in navigator) await navigator.serviceWorker.register('/sw.js')
}

function base64UrlToBytes(value: string): Uint8Array<ArrayBuffer> {
  const padded = (value + '='.repeat((4 - (value.length % 4)) % 4)).replace(/-/g, '+').replace(/_/g, '/')
  const raw = atob(padded)
  const bytes = new Uint8Array(new ArrayBuffer(raw.length))
  for (let i = 0; i < raw.length; i++) bytes[i] = raw.charCodeAt(i)
  return bytes
}

/** 必须在用户手势（点击）中调用：iOS 要求如此。 */
export async function enablePush(): Promise<void> {
  const permission = await Notification.requestPermission()
  if (permission !== 'granted') throw new Error(`通知权限未授予（${permission}）`)
  const registration = await navigator.serviceWorker.ready
  const { public_key } = await api.vapidKey()
  const existing = await registration.pushManager.getSubscription()
  const subscription =
    existing ??
    (await registration.pushManager.subscribe({
      userVisibleOnly: true,
      applicationServerKey: base64UrlToBytes(public_key),
    }))
  await api.subscribePush(subscription.toJSON())
}

/** 已授权时把当前订阅同步到服务端（订阅可能被浏览器轮换）。 */
export async function syncPushSubscription(): Promise<void> {
  if (pushState() !== 'granted') return
  const registration = await navigator.serviceWorker.ready
  const subscription = await registration.pushManager.getSubscription()
  if (subscription) await api.subscribePush(subscription.toJSON())
}

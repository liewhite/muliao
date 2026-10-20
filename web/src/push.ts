import { api, type Schemas } from './api'

/** 本设备的推送状态：有通知权限不等于在接收推送（退出登录时会取消订阅），所以「已授权」再分成有没有订阅 */
export type PushState = 'unsupported' | 'needs-install' | 'default' | 'denied' | 'unsubscribed' | 'subscribed'

type Permission = 'unsupported' | 'needs-install' | NotificationPermission

function isIos(): boolean {
  return /iPad|iPhone|iPod/.test(navigator.userAgent)
}

function isStandalone(): boolean {
  return window.matchMedia('(display-mode: standalone)').matches || ('standalone' in navigator && navigator.standalone === true)
}

function permission(): Permission {
  if (!('serviceWorker' in navigator)) return 'unsupported'
  // iOS 只有「添加到主屏幕」后以独立模式打开时才提供 PushManager
  if (!('PushManager' in window)) return isIos() && !isStandalone() ? 'needs-install' : 'unsupported'
  return Notification.permission
}

/** 本设备当前的订阅；没有注册 Service Worker 或没有订阅时为 null。不用 serviceWorker.ready：注册失败时它永远不返回 */
async function currentSubscription(): Promise<PushSubscription | null> {
  const registration = await navigator.serviceWorker.getRegistration()
  return (await registration?.pushManager.getSubscription()) ?? null
}

export async function pushState(): Promise<PushState> {
  const p = permission()
  if (p !== 'granted') return p
  return (await currentSubscription()) ? 'subscribed' : 'unsubscribed'
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
  // 不用 serviceWorker.ready：注册失败时它永远不返回，按钮会一直转圈
  const registration = await navigator.serviceWorker.getRegistration()
  if (registration === undefined) throw new Error('Service Worker 没有注册成功，刷新页面后再试')
  const { public_key } = await api.vapidKey()
  const existing = await registration.pushManager.getSubscription()
  const subscription =
    existing ??
    (await registration.pushManager.subscribe({
      userVisibleOnly: true,
      applicationServerKey: base64UrlToBytes(public_key),
    }))
  await api.subscribePush(subscriptionBody(subscription))
}

/** 订阅交给服务端的形状；浏览器给出的订阅缺密钥就是浏览器不合规，直接报错 */
function subscriptionBody(subscription: PushSubscription): Schemas['PushSubscriptionIn'] {
  const { p256dh, auth } = subscription.toJSON().keys ?? {}
  if (p256dh === undefined || auth === undefined) throw new Error('push subscription has no encryption keys')
  return { endpoint: subscription.endpoint, keys: { p256dh, auth } }
}

/** 取消本设备的推送订阅（退出登录时）；没有订阅时什么也不做 */
export async function unsubscribePush(): Promise<void> {
  if (permission() !== 'granted') return
  await (await currentSubscription())?.unsubscribe()
}

/** 已授权时把当前订阅同步到服务端（订阅可能被浏览器轮换）。 */
export async function syncPushSubscription(): Promise<void> {
  if (permission() !== 'granted') return
  const subscription = await currentSubscription()
  if (subscription) await api.subscribePush(subscriptionBody(subscription))
}

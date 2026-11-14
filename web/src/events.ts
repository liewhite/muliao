// 全局 SSE 连接。事件只用于实时展示；收到 changed 或重连时，页面从 API 重新拉取（数据库是唯一事实源，见 load.ts）。

import { useEffect, useRef, useSyncExternalStore } from 'react'
import { api, type Schemas } from './api'

/**
 * 连上了（前端自己产生），第一次连接与重连都算：订阅建立之前、断线期间都可能错过事件，页面要重新拉取。
 * 第一次也要算：页面的首次请求可能早于订阅建立，中间的事件（如运行结束）两边都没有，页面会一直停在旧状态。
 * 在这条连接的第一次心跳时发，不在 onopen：服务端先建好订阅再发心跳（http 的 events.rs），而响应头早于订阅，
 * 按 onopen 重新拉取可能落在订阅建立之前。
 */
export interface ConnectedEvent {
  type: 'connected'
}

/** 服务端定时发的心跳（http 的 events.rs），只用来判断连接是否还活着，不交给页面 */
type HeartbeatEvent = Schemas['HeartbeatEvent']

/** 交给页面的事件：服务端发的（除了心跳），加上前端自己产生的连上事件 */
export type PageEvent = Exclude<Schemas['ServerEvent'], HeartbeatEvent> | ConnectedEvent

type Listener = (e: PageEvent) => void

/** 这个页面跑的前端是否已经不是服务端当前的版本：页面加载后第一次心跳给出的版本，之后变了就是升级过 */
export function isOutdated(loadedVersion: string | null, serverVersion: string | null): boolean {
  return loadedVersion !== null && serverVersion !== null && loadedVersion !== serverVersion
}

/**
 * 连接是否已经死了：超过两个心跳周期没收到任何消息（再留 5 秒余量）。
 * 手机切后台、代理链路断开时 EventSource 往往收不到错误、以为还连着，事件就丢了，只能这样发现。
 */
export function isStale(lastMessageAt: number, now: number, intervalSeconds: number): boolean {
  return now - lastMessageAt > (2 * intervalSeconds + 5) * 1000
}

/**
 * 看门狗是否该丢掉当前连接重建：连接已被关闭（非 200 响应后 EventSource 不再自己重连），或已经知道心跳间隔、
 * 却太久没收到任何消息。还没收到过心跳时只看前者——第一次连接就失败的也要重建，不能永远停在那里。
 */
export function needsReconnect(closed: boolean, lastMessageAt: number, now: number, heartbeatSeconds: number | null): boolean {
  return closed || (heartbeatSeconds !== null && isStale(lastMessageAt, now, heartbeatSeconds))
}

const WATCHDOG_MS = 5000

const listeners = new Set<Listener>()
// 第一个订阅者来了之后为 true；连接本身在页面隐藏时断开（source 为 null）
let started = false
let source: EventSource | null = null
let lastMessageAt = 0
// 当前这条连接是否已经发过 connected（第一次心跳时发）
let announced = false
// 心跳间隔来自服务端的心跳；收到第一个之前不判断
let heartbeatSeconds: number | null = null
let watchdog: ReturnType<typeof setInterval> | null = null
// 页面加载后第一次心跳里的前端版本（这个页面跑的就是它）；之后的心跳与它不同就是服务端升级了
let loadedUiVersion: string | null | undefined
let outdated = false
const outdatedSubscribers = new Set<() => void>()

function connect() {
  source = new EventSource('/api/events')
  lastMessageAt = Date.now()
  announced = false
  source.onerror = (e) => {
    // 这条连接断了：EventSource 自己重连上之后的第一次心跳再发 connected
    announced = false
    // 网络错误时 EventSource 自己重连（readyState 回到 CONNECTING）；非 200 响应会让它永久关闭，由看门狗重建。
    // 关闭的原因可能是登录过期（401）：用一次普通请求走统一的 401 处理（回到登录页）
    if ((e.currentTarget as EventSource).readyState === EventSource.CLOSED) {
      console.warn('event stream closed by the server, the watchdog will reconnect')
      void api.me().catch((error) => console.error('checking the login after the event stream closed failed', error))
    }
  }
  source.onmessage = (msg) => {
    lastMessageAt = Date.now()
    const event = JSON.parse(msg.data) as Schemas['ServerEvent']
    if (event.type === 'heartbeat') {
      heartbeatSeconds = event.interval_seconds
      if (loadedUiVersion === undefined) loadedUiVersion = event.ui_version
      else if (!outdated && isOutdated(loadedUiVersion, event.ui_version)) {
        outdated = true
        outdatedSubscribers.forEach((s) => s())
      }
      if (!announced) {
        announced = true
        emit({ type: 'connected' })
      }
      return
    }
    emit(event)
  }
}

/** 交给每个 listener；一个抛错不能让后面的收不到，错误照常报告（控制台、window error 事件） */
function emit(event: PageEvent) {
  listeners.forEach((l) => {
    try {
      l(event)
    } catch (e) {
      reportError(e)
    }
  })
}

/** 连接已死就丢掉它重新连接；连上时页面收到 connected，从数据库重新拉取。页面在前台却没有连接（漏了回到前台的事件）也连上 */
function checkAlive() {
  if (source === null) {
    if (document.visibilityState === 'visible') connect()
    return
  }
  const closed = source.readyState === EventSource.CLOSED
  if (!needsReconnect(closed, lastMessageAt, Date.now(), heartbeatSeconds)) return
  console.warn(
    closed
      ? 'event stream is closed, reconnecting'
      : `no server event for ${Math.round((Date.now() - lastMessageAt) / 1000)}s, reconnecting`,
  )
  source.close()
  connect()
}

/** 隐藏多久才断开：来回切一下标签页不断开，也就不用回来时整页重拉 */
const HIDDEN_DISCONNECT_MS = 60_000
let hiddenTimer: ReturnType<typeof setTimeout> | null = null

/**
 * 页面隐藏（切到别的标签页、手机切后台）一分钟后断开连接：长时间没人看的页面不随 changed 一轮轮重拉数据。回到前台
 * 重新连接，第一次心跳时照常发 connected，页面按它拉一次，隐藏期间错过的变化都在里面。
 */
const onVisibilityChange = () => {
  if (hiddenTimer !== null) clearTimeout(hiddenTimer)
  hiddenTimer = null
  if (document.visibilityState === 'visible') {
    if (source === null) connect()
  } else {
    hiddenTimer = setTimeout(() => {
      hiddenTimer = null
      source?.close()
      source = null
    }, HIDDEN_DISCONNECT_MS)
  }
}

function start() {
  started = true
  if (document.visibilityState === 'visible') connect()
  watchdog = setInterval(checkAlive, WATCHDOG_MS)
  document.addEventListener('visibilitychange', onVisibilityChange)
}

/** 订阅页面事件（组件外用，如共享缓存）；第一个订阅者建立连接。返回取消订阅 */
export function listenServerEvents(listener: Listener): () => void {
  listeners.add(listener)
  if (!started) start()
  return () => {
    listeners.delete(listener)
  }
}

export function useServerEvents(listener: Listener) {
  const ref = useRef(listener)
  ref.current = listener
  useEffect(() => listenServerEvents((e) => ref.current(e)), [])
}

/**
 * 把 reload 包成「进行中最多再排一次」：运行中每步都在提交，changed 会一个接一个地来，请求不堆积；进行中又来的那些合并成
 * 它结束后的一次（那时才读，一定看得到它们）
 */
export function coalesced(reload: () => Promise<void>): () => void {
  let running = false
  let again = false
  const run = () => {
    if (running) {
      again = true
      return
    }
    running = true
    void reload()
      .catch((e) => console.error('refetching failed', e))
      .finally(() => {
        running = false
        if (again) {
          again = false
          run()
        }
      })
  }
  return run
}

/**
 * 第一次调用后等 waitMs 再执行一次 run，期间的调用都并进这一次；不因新的调用往后推（事件一直来也按 waitMs 一次执行）。
 * cancel 撤掉还没执行的那次。
 */
export function debounced(run: () => void, waitMs: number): { run: () => void; cancel: () => void } {
  let timer: ReturnType<typeof setTimeout> | null = null
  return {
    run: () => {
      timer ??= setTimeout(() => {
        timer = null
        run()
      }, waitMs)
    },
    cancel: () => {
      if (timer !== null) clearTimeout(timer)
      timer = null
    },
  }
}

/** 服务端已经换了前端版本，这个页面该刷新了（不自动刷新：会丢掉正在输入的草稿） */
export function useUiOutdated(): boolean {
  return useSyncExternalStore(
    (onChange) => {
      outdatedSubscribers.add(onChange)
      return () => {
        outdatedSubscribers.delete(onChange)
      }
    },
    () => outdated,
  )
}

export function closeServerEvents() {
  source?.close()
  source = null
  started = false
  if (hiddenTimer !== null) clearTimeout(hiddenTimer)
  hiddenTimer = null
  heartbeatSeconds = null
  if (watchdog !== null) clearInterval(watchdog)
  watchdog = null
  document.removeEventListener('visibilitychange', onVisibilityChange)
}

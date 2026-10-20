// 全局 SSE 连接。事件只用于实时展示；收到 task 状态变化或重连时，页面应从 API 重新拉取（数据库是唯一事实源）。

import { useEffect, useRef, useSyncExternalStore } from 'react'
import { api, type Item, type LiveRun, type Schemas } from './api'

/** 一条完整的会话条目（工具调用、工具结果），与历史同一个投影（后端 transcript）；文字可能截断过 */
export type RunItemEvent = Schemas['RunItemEvent']
/** 流式文字：接在同类的最后一条后面（或开始新的一条） */
export type RunDeltaEvent = Schemas['RunDeltaEvent']
/** run_id 与 seq：这一段的编号与事件序号（后端 bus.LiveRuns），快照与 SSE 事件据此合并、去重 */
export type RunEvent = RunItemEvent | RunDeltaEvent

export interface MergedLive {
  events: RunEvent[]
  /**
   * 快照与 SSE 之间缺了事件：打开页面时快照先生成、SSE 订阅后建立，中间的事件两边都没有。
   * 调用方应重新拉取快照（那时订阅已经建立，快照一定覆盖缺口）。
   */
  gap: boolean
}

/**
 * 以快照为准（这一段从开头起的全部事件），再接上之后经 SSE 收到、快照里还没有的同一段事件；别的段的事件丢弃
 * （上一段已保存，在历史里）。没有快照（运行已结束或还没开始）时保留已收到的事件，由随后的任务状态事件清空；
 * 收到的事件不是从这一段的开头起（快照生成时这一段还没开始、SSE 连上时已经开始了），同样是缺口。
 */
export function mergeLive(snapshot: LiveRun | null, current: RunEvent[]): MergedLive {
  if (snapshot === null) return { events: current, gap: current.length > 0 && current[0].seq !== 0 }
  const last = snapshot.events.at(-1)?.seq ?? -1
  const after = current.filter((e) => e.run_id === snapshot.run_id && e.seq > last)
  return { events: [...snapshot.events, ...after], gap: after.length > 0 && after[0].seq !== last + 1 }
}

/** 这一段的条目：完整条目原样接上，文字增量接在同类的最后一条后面（思考与回答交替时各自成条） */
export function liveItems(events: RunEvent[]): Item[] {
  const items: Item[] = []
  for (const e of events) {
    if (e.event === 'item') {
      items.push(e.item)
      continue
    }
    const last = items.at(-1)
    if (last?.type === e.item_type) items[items.length - 1] = { ...last, text: last.text + e.text }
    else
      items.push({
        type: e.item_type,
        text: e.text,
        tool_name: null,
        tool_call_id: null,
        message_id: null,
        attachments: [],
        threads: [],
        ask: null,
      })
  }
  return items
}

/** 重新连上了（前端自己产生）：期间可能丢了事件，页面要重新拉取 */
export interface ReconnectEvent {
  type: 'reconnected'
}

/** 服务端定时发的心跳（api/app.py），只用来判断连接是否还活着，不交给页面 */
type HeartbeatEvent = Schemas['HeartbeatEvent']

/** 交给页面的事件：服务端发的（除了心跳），加上前端自己的重连事件 */
export type PageEvent = Exclude<Schemas['ServerEvent'], HeartbeatEvent> | ReconnectEvent

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
let source: EventSource | null = null
// 是否连上过：之后的每次 open 都是重连，期间可能丢了事件，页面要重新拉取
let everOpened = false
let lastMessageAt = 0
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
  source.onopen = () => {
    if (everOpened) listeners.forEach((l) => l({ type: 'reconnected' }))
    everOpened = true
  }
  source.onerror = (e) => {
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
      return
    }
    listeners.forEach((l) => l(event))
  }
}

/** 连接已死就丢掉它重新连接；重连成功时页面收到 reconnected，从数据库重新拉取 */
function checkAlive() {
  if (source === null) return
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

const onVisible = () => {
  // 从后台回来时计时器可能还没跑，立刻检查
  if (document.visibilityState === 'visible') checkAlive()
}

function start() {
  connect()
  watchdog = setInterval(checkAlive, WATCHDOG_MS)
  document.addEventListener('visibilitychange', onVisible)
}

export function useServerEvents(listener: Listener) {
  const ref = useRef(listener)
  ref.current = listener
  useEffect(() => {
    const l: Listener = (e) => ref.current(e)
    listeners.add(l)
    if (source === null) start()
    return () => {
      listeners.delete(l)
    }
  }, [])
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
  everOpened = false
  heartbeatSeconds = null
  if (watchdog !== null) clearInterval(watchdog)
  watchdog = null
  document.removeEventListener('visibilitychange', onVisible)
}

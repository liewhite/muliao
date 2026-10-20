// 全局 SSE 连接。事件只用于实时展示；收到 task 状态变化或重连时，页面应从 API 重新拉取（数据库是唯一事实源）。

import { useEffect, useRef } from 'react'
import type { Attachment, Item, TaskKind, TaskStatus } from './api'

interface RunEventBase {
  type: 'run'
  session_id: string | null
  agent: string
  text: string
  /** 这一轮的编号与事件序号（后端 bus.LiveRuns）：快照与 SSE 事件据此合并、去重 */
  run_id: string
  seq: number
}

/** 回答与思考的增量片段：连续的同类片段拼成一条 */
export interface StreamEvent extends RunEventBase {
  event: 'text' | 'thinking'
}

export interface ToolEvent extends RunEventBase {
  event: 'tool_call' | 'tool_result' | 'tool_error'
  /** 后端 RetryPromptPart.tool_name 可以为空 */
  tool_name: string | null
  tool_call_id: string
  /** 工具结果挂给用户的文件（如 share_file）；调用事件为空 */
  attachments: Attachment[]
  /** 工具结果关联的子会话 id（委派）；调用事件为空 */
  threads: string[]
}

export type RunEvent = StreamEvent | ToolEvent

/** 增量片段按到达顺序拼成的条目类型；工具事件各自成条 */
const STREAMED = { text: 'assistant', thinking: 'thinking' } as const

/** 这一轮的快照（会话接口的 live 字段） */
export interface LiveRun {
  run_id: string
  events: RunEvent[]
}

export interface MergedLive {
  events: RunEvent[]
  /**
   * 快照与 SSE 之间缺了事件：打开页面时快照先生成、SSE 订阅后建立，中间的事件两边都没有。
   * 调用方应重新拉取快照（那时订阅已经建立，快照一定覆盖缺口）。
   */
  gap: boolean
}

/**
 * 以快照为准（这一轮从开头起的全部事件），再接上之后经 SSE 收到、快照里还没有的同一轮事件；别的轮次的事件丢弃。
 * 没有快照（这一轮已结束或还没开始）时保留已收到的事件，由随后的任务状态事件清空。
 */
export function mergeLive(snapshot: LiveRun | null, current: RunEvent[]): MergedLive {
  if (snapshot === null) return { events: current, gap: false }
  const last = snapshot.events.at(-1)?.seq ?? -1
  const after = current.filter((e) => e.run_id === snapshot.run_id && e.seq > last)
  return { events: [...snapshot.events, ...after], gap: after.length > 0 && after[0].seq !== last + 1 }
}

const isStream = (e: RunEvent): e is StreamEvent => e.event === 'text' || e.event === 'thinking'

/** 运行中的事件流转成与历史相同的展示条目：连续的同类增量拼成一条（思考与回答交替时各自成条） */
export function liveToItems(events: RunEvent[]): Item[] {
  const items: Item[] = []
  for (const e of events) {
    const last = items[items.length - 1]
    if (isStream(e)) {
      const type = STREAMED[e.event]
      if (last?.type === type) last.text += e.text
      else items.push({ type, text: e.text, tool_name: null, tool_call_id: null, message_id: null, attachments: [], threads: [] })
    } else {
      items.push({
        type: e.event,
        text: e.text,
        tool_name: e.tool_name,
        tool_call_id: e.tool_call_id,
        message_id: null,
        attachments: e.attachments,
        threads: e.threads,
      })
    }
  }
  return items
}

export interface TaskEvent {
  type: 'task'
  /** 内联跑完的私有 thread 轮次失败时没有 task 记录，为 null */
  task_id: number | null
  kind: TaskKind
  status: TaskStatus
  session_id: string | null
  /** 系统通知的投递、失败的内联轮次没有（或不知道）所属 agent */
  agent: string | null
  error: string | null
}

export interface ReconnectEvent {
  type: 'reconnected'
}

export type ServerEvent = RunEvent | TaskEvent | ReconnectEvent

type Listener = (e: ServerEvent) => void

/** 服务端定时发的心跳（api/app.py），只用来判断连接是否还活着，不交给页面 */
interface HeartbeatEvent {
  type: 'heartbeat'
  interval_seconds: number
}

/**
 * 连接是否已经死了：超过两个心跳周期没收到任何消息（再留 5 秒余量）。
 * 手机切后台、代理链路断开时 EventSource 往往收不到错误、以为还连着，事件就丢了，只能这样发现。
 */
export function isStale(lastMessageAt: number, now: number, intervalSeconds: number): boolean {
  return now - lastMessageAt > (2 * intervalSeconds + 5) * 1000
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

function connect() {
  source = new EventSource('/api/events')
  lastMessageAt = Date.now()
  source.onopen = () => {
    if (everOpened) listeners.forEach((l) => l({ type: 'reconnected' }))
    everOpened = true
  }
  source.onmessage = (msg) => {
    lastMessageAt = Date.now()
    const event = JSON.parse(msg.data) as ServerEvent | HeartbeatEvent
    if (event.type === 'heartbeat') {
      heartbeatSeconds = event.interval_seconds
      return
    }
    listeners.forEach((l) => l(event))
  }
}

/** 连接已死就丢掉它重新连接；重连成功时页面收到 reconnected，从数据库重新拉取 */
function checkAlive() {
  if (source === null || heartbeatSeconds === null || !isStale(lastMessageAt, Date.now(), heartbeatSeconds)) return
  console.warn(`no server event for ${Math.round((Date.now() - lastMessageAt) / 1000)}s, reconnecting`)
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

export function closeServerEvents() {
  source?.close()
  source = null
  everOpened = false
  heartbeatSeconds = null
  if (watchdog !== null) clearInterval(watchdog)
  watchdog = null
  document.removeEventListener('visibilitychange', onVisible)
}

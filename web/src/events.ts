// 全局 SSE 连接。事件只用于实时展示；收到 task 状态变化或重连时，页面应从 API 重新拉取（数据库是唯一事实源）。

import { useEffect, useRef } from 'react'

export interface RunEvent {
  type: 'run'
  session_id: string | null
  agent: string
  event: 'text' | 'tool_call' | 'tool_result' | 'tool_error'
  text: string
  tool_name?: string | null
  tool_call_id?: string | null
}

export interface TaskEvent {
  type: 'task'
  task_id: number
  kind: 'turn' | 'reflection' | 'deliver' | 'compaction'
  status: 'running' | 'pending' | 'done' | 'failed'
  session_id: string | null
  agent: string
  error: string | null
}

export interface ReconnectEvent {
  type: 'reconnected'
}

export type ServerEvent = RunEvent | TaskEvent | ReconnectEvent

type Listener = (e: ServerEvent) => void

const listeners = new Set<Listener>()
let source: EventSource | null = null

function connect() {
  source = new EventSource('/api/events')
  let opened = false
  source.onopen = () => {
    // 首次连接之外的 open 都是重连，期间可能丢事件
    if (opened) listeners.forEach((l) => l({ type: 'reconnected' }))
    opened = true
  }
  source.onmessage = (msg) => {
    const event = JSON.parse(msg.data) as ServerEvent
    listeners.forEach((l) => l(event))
  }
}

export function useServerEvents(listener: Listener) {
  const ref = useRef(listener)
  ref.current = listener
  useEffect(() => {
    const l: Listener = (e) => ref.current(e)
    listeners.add(l)
    if (source === null) connect()
    return () => {
      listeners.delete(l)
    }
  }, [])
}

export function closeServerEvents() {
  source?.close()
  source = null
}

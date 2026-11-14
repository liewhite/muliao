// 会话页上进行中的那次运行：会话快照里的这一段，接上经 SSE 收到、快照里还没有的事件

import { useEffect, useState } from 'react'
import type { Item, LiveSegment, Schemas, SessionDetail, SessionId } from '../api'
import { useServerEvents } from '../events'
import type { Loaded } from '../load'

/**
 * 运行中的一条实时事件，按 event 区分：item 是一条完整的会话条目（工具调用、工具结果，与历史同一个投影，文字可能截断过）；
 * delta 是流式文字，接在同类的最后一条后面（或开始新的一条）。segment_id 与 seq：这一段的编号与事件序号，快照与 SSE
 * 事件据此合并、去重
 */
export type RunEvent = Schemas['RunEventEnvelope']

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
 * （上一段已保存，在历史里）。没有快照（运行已结束或还没开始）时保留已收到的事件，由重新拉回的、没有进行中运行的快照清空（useLiveRun）；
 * 收到的事件不是从这一段的开头起（快照生成时这一段还没开始、SSE 连上时已经开始了），同样是缺口。
 */
export function mergeLive(snapshot: LiveSegment | null, current: RunEvent[]): MergedLive {
  if (snapshot === null) return { events: current, gap: current.length > 0 && current[0].seq !== 0 }
  const last = snapshot.events.at(-1)?.seq ?? -1
  const after = current.filter((e) => e.segment_id === snapshot.segment_id && e.seq > last)
  return { events: [...snapshot.events, ...after], gap: after.length > 0 && after[0].seq !== last + 1 }
}

/**
 * 收到新事件 next 时，已收到的事件里还要留着的，免得一轮长运行里越积越多。next 与快照同一段时，快照已覆盖的、
 * 更早的段的都可以删（SSE 按顺序到达，之前收到的不会比 next 新）。next 是快照之后的新一段（保存了一步、新快照还没
 * 拉回来）或还没有快照时全部留着：新快照可能生成得比这些事件早，要靠它们接上。
 */
export function withoutCovered(snapshot: LiveSegment | null, current: RunEvent[], next: RunEvent): RunEvent[] {
  if (snapshot === null || next.segment_id !== snapshot.segment_id) return current
  const last = snapshot.events.at(-1)?.seq ?? -1
  return current.filter((e) => e.segment_id === snapshot.segment_id && e.seq > last)
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
    // 还在流式生成的回答不知道是不是占位答复（由服务端判断，保存后以历史为准）
    const item = (text: string): Item =>
      e.item_type === 'assistant' ? { type: 'assistant', text, no_progress: false } : { type: 'thinking', text }
    if (last?.type === e.item_type) items[items.length - 1] = item(last.text + e.text)
    else items.push(item(e.text))
  }
  return items
}

/**
 * 会话页显示的运行中条目。中途打开或重新拉取时以快照为准，从这一段的开头显示，再接上经 SSE 收到、快照里还没有的事件；
 * 接不上（mergeLive 的 gap）就重新拉一次快照。重新拉取本身由会话页的 Loader 负责（又保存了一步：新快照换成新的一段，
 * 合并时丢掉上一段的事件）
 */
export function useLiveRun(id: SessionId, detail: Loaded<SessionDetail>): Item[] {
  const [live, setLive] = useState<RunEvent[]>([])
  useServerEvents((e) => {
    if (e.type === 'connected') setLive([])
    else if (e.type === 'run' && e.session_id === id)
      setLive((prev) => [...withoutCovered(detail.data?.live ?? null, prev, e), e])
  })

  // 重新拉取到的快照里没有进行中的运行（运行或压缩结束了）：以数据库为准，清空临时流。快照生成后才开始的运行，
  // 已收到的事件被清掉时下一条事件接不上（mergeLive 的 gap），会再拉一次；清掉到下一条事件之间画面暂时缺这几条，是预期的
  // （渲染时按「快照换了」调整，React 推荐的做法，不用 effect）
  const [snapshot, setSnapshot] = useState(detail.data)
  if (detail.data !== snapshot) {
    setSnapshot(detail.data)
    if (detail.data?.live === null) setLive([])
  }

  const merged = mergeLive(detail.data?.live ?? null, live)
  const reload = detail.reload
  useEffect(() => {
    if (merged.gap) void reload()
  }, [merged.gap, reload])
  return liveItems(merged.events)
}

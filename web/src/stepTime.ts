// 步骤（工具调用）的开始时间与耗时：时间来自后端条目上的 at（调用开始执行、结果产生的时刻），前端只计算与格式化，
// 不拿收到事件的本地时间推算。拿不到的不显示，不编默认值。

import { useEffect, useState } from 'react'
import { toolStatus, type Step, type ToolView } from './blocks'

/** 一个步骤或一组步骤的时间段，毫秒时间戳。running：还在执行（显示已运行时长）；end 为 null 且不在运行：结束时间未知 */
export interface Span {
  start: number
  end: number | null
  running: boolean
}

export function toolSpan(tool: ToolView): Span | null {
  if (tool.startedAt === null) return null
  const running = toolStatus(tool) === 'running'
  return {
    start: Date.parse(tool.startedAt),
    end: running || tool.finishedAt === null ? null : Date.parse(tool.finishedAt),
    running,
  }
}

/** 一组步骤：从最早开始的工具算起；有工具在运行就是运行中；每个有开始时间的工具都知道结束时间才给出结束（取最晚的） */
export function groupSpan(steps: Step[]): Span | null {
  const spans = steps.flatMap((s) => (s.kind === 'tool' ? [toolSpan(s.tool)] : [])).filter((s) => s !== null)
  if (spans.length === 0) return null
  const running = spans.some((s) => s.running)
  const ends = spans.map((s) => s.end)
  return {
    start: Math.min(...spans.map((s) => s.start)),
    end: running || ends.some((e) => e === null) ? null : Math.max(...(ends as number[])),
    running,
  }
}

/** 4.2s、42s、1m23s、1h02m */
export function formatDuration(ms: number): string {
  const s = Math.max(0, ms) / 1000
  if (s < 10) return `${s.toFixed(1)}s`
  if (s < 60) return `${Math.floor(s)}s`
  const m = Math.floor(s / 60)
  if (m < 60) return `${m}m${String(Math.floor(s % 60)).padStart(2, '0')}s`
  return `${Math.floor(m / 60)}h${String(m % 60).padStart(2, '0')}m`
}

/** 本地时间 HH:MM:SS；不是今天的前面加上 M-D */
export function formatClock(at: number, now: number): string {
  const d = new Date(at)
  const pad = (n: number) => String(n).padStart(2, '0')
  const time = `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
  return new Date(now).toDateString() === d.toDateString() ? time : `${d.getMonth() + 1}-${d.getDate()} ${time}`
}

/** 运行中：「已运行 1m23s」；已结束：「13:05:12 · 4.2s」；结束时间未知：只有开始时间 */
export function spanLabel(span: Span, now: number): string {
  if (span.running) return `已运行 ${formatDuration(now - span.start)}`
  const start = formatClock(span.start, now)
  return span.end === null ? start : `${start} · ${formatDuration(span.end - span.start)}`
}

/** 当前时间（毫秒）；active 时每秒刷新一次，用于运行中步骤的计时 */
export function useNow(active: boolean): number {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    if (!active) return
    const timer = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(timer)
  }, [active])
  return now
}

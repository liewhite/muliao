// 步骤（工具调用）的开始时间与耗时：时间来自后端条目上的 at（调用开始执行、结果产生的时刻），前端只计算与格式化，
// 不拿收到事件的本地时间推算。拿不到的不显示，不编默认值。

import { useEffect, useState } from 'react'
import { toolStatus, type Step, type ToolView } from './blocks'

/**
 * 一个步骤或一组步骤的耗时，毫秒。组的耗时是组内各工具耗时之和（并行的也各自累加），不是首个开始到最后结束的墙钟时间，
 * 工具之间模型思考、生成的时间不算。
 * start：最早开始的时刻；spent：已结束的工具耗时之和；runningSince：正在执行的工具各自的开始时刻（显示已运行时长）；
 * unknown：有不在执行、结束时间却未知的工具（这时已结束的组只显示开始时间）
 */
export interface Span {
  start: number
  spent: number
  runningSince: number[]
  unknown: boolean
}

export const isRunning = (span: Span | null): boolean => (span?.runningSince.length ?? 0) > 0

/**
 * live：条目是否来自正在进行的运行。历史里没有结果的调用（执行时失败、等重试或续跑）并没有在执行，不计时，只显示开始时间
 */
export function toolSpan(tool: ToolView, live: boolean): Span | null {
  if (tool.startedAt === null) return null
  const start = Date.parse(tool.startedAt)
  if (live && toolStatus(tool) === 'running') return { start, spent: 0, runningSince: [start], unknown: false }
  if (tool.result === null || tool.finishedAt === null) return { start, spent: 0, runningSince: [], unknown: true }
  return { start, spent: Date.parse(tool.finishedAt) - start, runningSince: [], unknown: false }
}

/** 一组步骤：从最早开始的工具算起，耗时为各工具耗时之和，与展开后各行相加一致 */
export function groupSpan(steps: Step[], live: boolean): Span | null {
  const spans = steps.flatMap((s) => (s.kind === 'tool' ? [toolSpan(s.tool, live)] : [])).filter((s) => s !== null)
  if (spans.length === 0) return null
  return {
    start: Math.min(...spans.map((s) => s.start)),
    spent: spans.reduce((sum, s) => sum + s.spent, 0),
    runningSince: spans.flatMap((s) => s.runningSince),
    unknown: spans.some((s) => s.unknown),
  }
}

/** 4.2s、42s、1m23s、1h02m；whole：每秒刷新的计时只到整秒 */
export function formatDuration(ms: number, whole = false): string {
  const s = ms / 1000
  if (s < 10 && !whole) return `${s.toFixed(1)}s`
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

/**
 * 运行中：「已运行 1m23s」，已结束的耗时加上正在执行的已运行时长，组结束时数字连续；
 * 已结束：「13:05:12 · 4.2s」；有结束时间未知的：只有开始时间
 */
export function spanLabel(span: Span, now: number): string {
  if (isRunning(span)) {
    // 浏览器与服务端可能不是同一台机器，时钟的偏差可能让刚开始的调用算出负数
    const elapsed = span.runningSince.reduce((sum, s) => sum + Math.max(0, now - s), span.spent)
    return `已运行 ${formatDuration(elapsed, true)}`
  }
  const start = formatClock(span.start, now)
  return span.unknown ? start : `${start} · ${formatDuration(span.spent)}`
}

/** 当前时间（毫秒）；active 时每秒刷新一次，用于运行中步骤的计时 */
export function useNow(active: boolean): number {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    if (!active) return
    // 挂载时取的 now 可能早就旧了（过程组里后来又开始了新的调用），开始计时时先刷新一次
    const tick = () => setNow(Date.now())
    const first = setTimeout(tick, 0)
    const timer = setInterval(tick, 1000)
    return () => {
      clearTimeout(first)
      clearInterval(timer)
    }
  }, [active])
  return now
}

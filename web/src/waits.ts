import type { Wait } from './api'
import { formatClock, formatDuration } from './stepTime'

/** 提问条目的锚点：等待项里的「跳到提问」滚到这里 */
export const askAnchor = (callId: string) => `ask-${callId}`

/** 等待项的稳定 key：前面的项消失时，后面一行的展开状态不串到别的行 */
export function waitKey(w: Wait): string {
  switch (w.kind) {
    case 'job':
      return `job:${w.job_id}`
    case 'delegation':
      return `delegation:${w.thread_session_id ?? 'gone'}:${w.since}`
    case 'wake':
      return `wake:${w.due_at}`
    case 'ask':
      return `ask:${w.call_id}`
  }
}

/** 从 iso 到 now 过了多久（时钟偏差算出负数时当 0） */
const elapsed = (iso: string, now: number) => formatDuration(Math.max(0, now - Date.parse(iso)), true)

/** 等待项的说明：等了多久、还要多久、按什么节奏检查（每秒随 now 刷新） */
export function waitDetail(w: Wait, now: number): string {
  switch (w.kind) {
    case 'job':
      return `已运行 ${elapsed(w.since, now)}`
    case 'delegation': {
      const waited = `已等 ${elapsed(w.since, now)}`
      if (w.deadline !== null) {
        const left = Date.parse(w.deadline) - now
        return `ask_agent · ${left > 0 ? `超时还剩 ${formatDuration(left, true)}` : '已到超时，正在停止'} · ${waited}`
      }
      // 委派要么阻塞（deadline）要么并发（心跳），后端保证二者有其一
      if (w.check_seconds === null) throw new Error('delegation wait has neither a deadline nor a heartbeat')
      return `report_back · 每 ${formatDuration(w.check_seconds * 1000, true)} 检查 · ${waited}`
    }
    case 'wake': {
      const due = Date.parse(w.due_at)
      return `${formatClock(due, now)}${due > now ? `（还有 ${formatDuration(due - now, true)}）` : '（已到点）'}`
    }
    case 'ask':
      return w.headers.join('、')
  }
}

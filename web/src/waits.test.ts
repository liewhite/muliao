import { describe, expect, it } from 'vitest'
import type { Wait } from './api'
import { waitDetail } from './waits'

const now = Date.parse('2026-10-29T12:00:00Z')
const ago = (s: number) => new Date(now - s * 1000).toISOString()
const later = (s: number) => new Date(now + s * 1000).toISOString()

describe('waitDetail', () => {
  it('shows how long a job has been running', () => {
    const w: Wait = { kind: 'job', job_id: 3, since: ago(75), command: 'sleep 99', status: 'running', log_path: 'jobs/3.log', tail: '' }
    expect(waitDetail(w, now)).toBe('已运行 1m15s')
  })

  it('shows the time left before an ask_agent times out, and when it has run out', () => {
    const w: Wait = { kind: 'delegation', thread_session_id: 't', check_seconds: null, deadline: later(90), since: ago(30) }
    expect(waitDetail(w, now)).toBe('ask_agent · 超时还剩 1m30s · 已等 30s')
    expect(waitDetail({ ...w, deadline: ago(1) }, now)).toBe('ask_agent · 已到超时，正在停止 · 已等 30s')
  })

  it('shows the heartbeat of a report_back delegation', () => {
    const w: Wait = { kind: 'delegation', thread_session_id: 't', check_seconds: 300, deadline: null, since: ago(5) }
    expect(waitDetail(w, now)).toBe('report_back · 每 5m00s 检查 · 已等 5s')
  })

  it('rejects a delegation that is neither blocking nor concurrent', () => {
    const w: Wait = { kind: 'delegation', thread_session_id: 't', check_seconds: null, deadline: null, since: ago(5) }
    expect(() => waitDetail(w, now)).toThrow('neither')
  })

  it('shows when a wake-up is due and how far away it is', () => {
    const w: Wait = { kind: 'wake', due_at: later(3600), note: 'check', since: ago(10) }
    expect(waitDetail(w, now)).toMatch(/（还有 1h00m）$/)
    expect(waitDetail({ ...w, due_at: ago(1) }, now)).toMatch(/（已到点）$/)
  })

  it('lists the headers of a pending question', () => {
    expect(waitDetail({ kind: 'ask', call_id: 'c', headers: ['Scope', 'When'] }, now)).toBe('Scope、When')
  })
})

import { describe, expect, it } from 'vitest'
import { cacheHitPercent, formatTokens, windowOf } from './usage'

describe('windowOf', () => {
  it('starts at local midnight so the first day is a whole day', () => {
    const now = new Date(2026, 2, 10, 15, 30)
    const [since, until] = windowOf(7, now)
    expect([since.getFullYear(), since.getMonth(), since.getDate()]).toEqual([2026, 2, 4])
    expect([since.getHours(), since.getMinutes(), since.getSeconds()]).toEqual([0, 0, 0])
    expect(until).toBe(now)
  })

  it('counts today as one of the days', () => {
    const [since] = windowOf(1, new Date(2026, 2, 10, 15, 30))
    expect(since.getDate()).toBe(10)
  })

  it('crosses a month boundary', () => {
    const [since] = windowOf(30, new Date(2026, 2, 10, 15, 30))
    expect([since.getMonth(), since.getDate()]).toEqual([1, 9])
  })
})

describe('formatTokens', () => {
  it('abbreviates by magnitude', () => {
    expect(formatTokens(0)).toBe('0')
    expect(formatTokens(999)).toBe('999')
    expect(formatTokens(1500)).toBe('1.5K')
    expect(formatTokens(45_600)).toBe('46K')
    expect(formatTokens(2_400_000)).toBe('2.4M')
  })
})

describe('cacheHitPercent', () => {
  it('is the cached share of the input total', () => {
    expect(cacheHitPercent(100, 80)).toBe(80)
    // 没有输入时不除零
    expect(cacheHitPercent(0, 0)).toBe(0)
  })
})

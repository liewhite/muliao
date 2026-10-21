import { describe, expect, it } from 'vitest'
import { inAnHour, wallClock } from './wallClock'

describe('wallClock', () => {
  it('keeps the server-side wall clock time and drops the offset', () => {
    expect(wallClock('2026-10-22T10:30:00+08:00')).toBe('2026-10-22T10:30')
    expect(wallClock('2026-01-02T09:05:00+00:00')).toBe('2026-01-02T09:05')
  })
})

describe('inAnHour', () => {
  it('is an hour later, zero padded so the input accepts it', () => {
    expect(inAnHour(new Date(2026, 0, 2, 8, 5))).toBe('2026-01-02T09:05')
  })

  it('rolls over into the next day', () => {
    expect(inAnHour(new Date(2026, 11, 31, 23, 30))).toBe('2027-01-01T00:30')
  })
})

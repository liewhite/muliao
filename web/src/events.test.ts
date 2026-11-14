import { describe, expect, it } from 'vitest'
import { isOutdated, isStale, needsReconnect } from './events'

describe('isStale', () => {
  it('keeps a connection that heard anything within two heartbeats plus slack', () => {
    expect(isStale(0, 35_000, 15)).toBe(false)
  })
  it('gives up on a connection silent for longer, e.g. after the phone was in the background', () => {
    expect(isStale(0, 35_001, 15)).toBe(true)
    expect(isStale(0, 10 * 60_000, 15)).toBe(true)
  })
})

describe('needsReconnect', () => {
  it('rebuilds a connection the server closed, even before the first heartbeat', () => {
    // 第一次连接就收到 502（服务重启中）：EventSource 不再自己重连
    expect(needsReconnect(true, 0, 1, null)).toBe(true)
  })
  it('does not judge silence before knowing the heartbeat interval', () => {
    expect(needsReconnect(false, 0, 10 * 60_000, null)).toBe(false)
  })
  it('rebuilds a silent connection once the interval is known', () => {
    expect(needsReconnect(false, 0, 35_001, 15)).toBe(true)
    expect(needsReconnect(false, 0, 35_000, 15)).toBe(false)
  })
})

describe('isOutdated', () => {
  it('flags a page whose build differs from the one the server now serves', () => {
    expect(isOutdated('aaa', 'bbb')).toBe(true)
    expect(isOutdated('aaa', 'aaa')).toBe(false)
  })
  it('never flags when either side has no frontend build', () => {
    expect(isOutdated(null, 'bbb')).toBe(false)
    expect(isOutdated('aaa', null)).toBe(false)
  })
})

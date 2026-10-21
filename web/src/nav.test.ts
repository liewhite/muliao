import { describe, expect, it } from 'vitest'
import { isAdminPath } from './nav'

describe('isAdminPath', () => {
  it('管理菜单里的页面及其子页面属于管理', () => {
    expect(isAdminPath('/agents')).toBe(true)
    expect(isAdminPath('/agents/abc')).toBe(true)
    expect(isAdminPath('/skills/edit')).toBe(true)
    expect(isAdminPath('/settings')).toBe(true)
  })

  it('动态页面与前缀相似的路径不属于管理', () => {
    expect(isAdminPath('/')).toBe(false)
    expect(isAdminPath('/sessions/x')).toBe(false)
    expect(isAdminPath('/issues/3')).toBe(false)
    expect(isAdminPath('/agentsx')).toBe(false)
  })
})

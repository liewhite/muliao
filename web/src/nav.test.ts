import { describe, expect, it } from 'vitest'
import { backTarget, isAdminPath, returnTo } from './nav'

describe('isAdminPath', () => {
  it('管理菜单里的页面及其子页面属于管理', () => {
    expect(isAdminPath('/agents')).toBe(true)
    expect(isAdminPath('/agents/abc')).toBe(true)
    expect(isAdminPath('/items/skill')).toBe(true)
    expect(isAdminPath('/settings')).toBe(true)
  })

  it('动态页面与前缀相似的路径不属于管理', () => {
    expect(isAdminPath('/')).toBe(false)
    expect(isAdminPath('/sessions/x')).toBe(false)
    expect(isAdminPath('/issues/3')).toBe(false)
    expect(isAdminPath('/agentsx')).toBe(false)
  })
})

describe('backTarget', () => {
  it('来源页带了 ReturnState 就回来源页（含查询串），否则回固定的上级页', () => {
    const state = returnTo({ pathname: '/issues/12', search: '?review=reject' })
    expect(backTarget(state, '/')).toBe('/issues/12?review=reject')
    expect(backTarget(null, '/')).toBe('/')
    expect(backTarget({ other: 1 }, '/issues')).toBe('/issues')
  })
})

import { describe, expect, expectTypeOf, it } from 'vitest'
import { FILES_PATH, api, asAgentId, href, type Agent, type AgentId, type Memory, type Schemas, type SessionId } from './api'
import type { paths } from './schema'

describe('api calls are typed from the OpenAPI paths', () => {
  it('infers response bodies, no-content responses and branded agent ids', () => {
    expectTypeOf(api.memory).returns.resolves.toEqualTypeOf<Memory>()
    expectTypeOf(api.unreadNotifications).returns.resolves.toEqualTypeOf<Schemas['UnreadOut']>()
    expectTypeOf(api.stop).returns.resolves.toEqualTypeOf<void>()
    expectTypeOf(api.agents).returns.resolves.toEqualTypeOf<Agent[]>()
  })

  it('brands the ids from the generated types: one kind of id cannot stand for another or for a plain string', () => {
    expectTypeOf<Agent['id']>().toEqualTypeOf<AgentId>()
    expectTypeOf<Schemas['SessionOut']['agent_id']>().toEqualTypeOf<AgentId>()
    expectTypeOf<SessionId>().not.toExtend<AgentId>()
    expectTypeOf<string>().not.toExtend<SessionId>()
  })

  it('FILES_PATH is a path the backend serves', () => {
    expectTypeOf<typeof FILES_PATH>().toExtend<keyof paths>()
  })

  it('href fills path parameters and query from the same typed arguments as call', () => {
    const url = href('/api/agents/{agent_id}/workspace/file', {
      path: { agent_id: asAgentId('a/b') },
      query: { path: 'site/x y.png' },
    })
    expect(url).toBe('/api/agents/a%2Fb/workspace/file?path=site%2Fx+y.png')
    // 只有 GET 接口有地址可打开；路径参数不能漏
    // @ts-expect-error 没有 GET 的接口
    href('/api/auth/login')
    // @ts-expect-error 缺路径参数
    expect(() => href('/api/agents/{agent_id}/workspace/file', { query: { path: 'x' } })).toThrow()
  })
})

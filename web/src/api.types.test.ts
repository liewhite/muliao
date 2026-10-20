import { describe, expectTypeOf, it } from 'vitest'
import { api, branded, call, type Agent, type Memory, type Schemas } from './api'

describe('api calls are typed from the OpenAPI paths', () => {
  it('infers response bodies, no-content responses and branded agent ids', () => {
    expectTypeOf(api.memory).returns.resolves.toEqualTypeOf<Memory>()
    expectTypeOf(api.unreadNotifications).returns.resolves.toEqualTypeOf<Schemas['UnreadOut']>()
    expectTypeOf(api.stop).returns.resolves.toEqualTypeOf<void>()
    expectTypeOf(api.agents).returns.resolves.toEqualTypeOf<Agent[]>()
  })

  it('only brands agent ids: a response of another shape cannot be branded as agents', () => {
    // @ts-expect-error 触发器列表不是 agent 列表
    void (() => branded<Agent[]>(call('get', '/api/triggers')))
  })
})

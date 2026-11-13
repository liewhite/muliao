import { describe, expectTypeOf, it } from 'vitest'
import { FILES_PATH, api, type Agent, type AgentId, type Memory, type Schemas, type SessionId } from './api'
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
})

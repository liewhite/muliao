import { afterEach, describe, expect, it, vi } from 'vitest'
import { api } from './api'

function respond(status: number, body: string, headers: Record<string, string> = {}) {
  vi.stubGlobal('fetch', vi.fn(async () => new Response(status === 204 ? null : body, { status, headers })))
}

const JSON_TYPE = { 'Content-Type': 'application/json' }

describe('send decodes responses by HTTP status and type', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('treats 204 as no content even with the JSON content type FastAPI sends', async () => {
    respond(204, '', JSON_TYPE)
    await expect(api.logout()).resolves.toBeUndefined()
  })

  it('returns nothing for a 202 without a body', async () => {
    respond(202, '')
    await expect(api.stop('s1')).resolves.toBeUndefined()
  })

  it('decodes a JSON body', async () => {
    respond(202, '{"queued": true}', JSON_TYPE)
    await expect(api.compact('s1')).resolves.toEqual({ queued: true })
  })

  it('fails when a declared JSON body is missing', async () => {
    respond(200, '', JSON_TYPE)
    await expect(api.memory('a1' as never)).rejects.toThrow()
  })

  it('fails when a response without a declared body has one', async () => {
    respond(202, '<html>proxy page</html>', { 'Content-Type': 'text/html' })
    await expect(api.stop('s1')).rejects.toThrow(/unexpected non-JSON body/)
  })
})

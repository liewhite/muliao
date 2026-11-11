import { readFile } from 'node:fs/promises'
import { expect, it } from 'vitest'
import { generate, generateReads, OUTPUT, READS_OUTPUT, relaxRequestDefaults } from './gen-api.mjs'

it('the committed frontend types match openapi.json', async () => {
  // 不一致时运行 npm run gen:api
  expect(await readFile(OUTPUT, 'utf8')).toBe(await generate())
  expect(await readFile(READS_OUTPUT, 'utf8')).toBe(await generateReads())
})

it('only request-only schemas lose their defaults', () => {
  const prop = () => ({ type: 'string', default: 'x' })
  const ref = (n) => ({ $ref: `#/components/schemas/${n}` })
  const body = (n) => ({ requestBody: { content: { 'application/json': { schema: ref(n) } } } })
  const reply = (n) => ({ responses: { 200: { content: { 'application/json': { schema: ref(n) } } } } })
  const spec = {
    paths: { '/a': { post: { ...body('In'), ...reply('Out') } }, '/b': { post: body('Shared') } },
    components: {
      schemas: {
        In: { properties: { f: prop(), nested: ref('Nested') } },
        Nested: { properties: { f: prop() } },
        Out: { properties: { f: prop(), shared: ref('Shared') } },
        Shared: { properties: { f: prop() } },
        Event: { properties: { f: prop() } },
      },
    },
  }
  const s = relaxRequestDefaults(spec).components.schemas
  expect([s.In, s.Nested, s.Out, s.Shared, s.Event].map((x) => 'default' in x.properties.f)).toEqual([false, false, true, true, true])
})

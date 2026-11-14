import { readFile } from 'node:fs/promises'
import { expect, it } from 'vitest'
import { brandedShapes, generate, OUTPUT, optionalResponseFields, relaxRequestDefaults } from './gen-api.mjs'

it('the committed frontend types match openapi.json', async () => {
  // 不一致时运行 npm run gen:api
  expect(await readFile(OUTPUT, 'utf8')).toBe(await generate())
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

it('every field of a response type is always there', async () => {
  // 有不必有的字段时，后端给响应拆一个字段全必有的 -Out 类型（如 PrecheckOut），别让前端写兜底
  const spec = JSON.parse(await readFile(new URL('../openapi.json', import.meta.url), 'utf8'))
  expect(optionalResponseFields(spec)).toEqual([])
})

it('finds optional fields only in response types', () => {
  const ref = (n) => ({ $ref: `#/components/schemas/${n}` })
  const spec = {
    paths: {
      '/a': {
        post: {
          requestBody: { content: { 'application/json': { schema: ref('In') } } },
          responses: { 200: { content: { 'application/json': { schema: ref('Out') } } } },
        },
      },
    },
    components: {
      schemas: {
        In: { properties: { f: {} } },
        Out: { required: ['a'], properties: { a: {}, b: {}, inner: { oneOf: [{ properties: { c: {} } }] } } },
      },
    },
  }
  expect(optionalResponseFields(spec)).toEqual(['Out.b', 'Out.inner', 'Out.inner.c'])
})

it('brands id types by name, and recognizes them where a generic inlined them', async () => {
  const shapes = brandedShapes({
    IssueId: { type: 'integer', format: 'int64', description: 'issue' },
    AgentId: { type: 'string', description: 'agent' },
    Note: { type: 'string', description: 'note' },
    Change_Vec_IssueId: { type: 'object' },
  })
  expect([...shapes.values()].sort()).toEqual(['AgentId', 'IssueId'])
  // 后端泛型 Change<Vec<IssueId>> 里的编号被就地展开，生成的类型照样带品牌
  const generated = await readFile(OUTPUT, 'utf8')
  expect(/Change_Vec_IssueId: \{\s*from: \(number & \{\s*readonly __brand: 'IssueId'/.test(generated)).toBe(true)
})

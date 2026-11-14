// 从后端的接口描述（openapi.json，后端测试保证它与接口一致）生成前端类型 src/schema.d.ts；scripts/gen-api.test.mjs 保证
// 提交的生成结果与描述一致。用法：npm run gen:api
//
// openapi-typescript 7.13 的 peer 依赖写的是 TypeScript 5，项目用 6：package.json 的 overrides 让它用项目的
// TypeScript（它只用编译器 API 打印类型，实测生成正常）。它声明支持 TS 6 之后删掉这条 override。

import { readFile, writeFile } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'
import openapiTS, { astToString } from 'openapi-typescript'
import ts from 'typescript'

const SPEC = new URL('../openapi.json', import.meta.url)
export const OUTPUT = new URL('../src/schema.d.ts', import.meta.url)

const HEADER = '// 由 scripts/gen-api.mjs 从 openapi.json 生成，不要手改\n\n'

const PREFIX = '#/components/schemas/'

/** node 里（不进入 skip 挑出的子树）引用到的全部 schema 名 */
function refsIn(node, skip = () => false, found = new Set()) {
  if (Array.isArray(node)) node.forEach((n) => refsIn(n, skip, found))
  else if (node && typeof node === 'object') {
    if (typeof node.$ref === 'string' && node.$ref.startsWith(PREFIX)) found.add(node.$ref.slice(PREFIX.length))
    for (const [k, v] of Object.entries(node)) if (!skip(k)) refsIn(v, skip, found)
  }
  return found
}

/** 从 roots 出发经引用能到的全部 schema */
function closure(schemas, roots) {
  const seen = new Set()
  const todo = [...roots]
  while (todo.length) {
    const name = todo.pop()
    if (seen.has(name)) continue
    seen.add(name)
    todo.push(...refsIn(schemas[name]))
  }
  return seen
}

/**
 * 只出现在请求体里的类型：有默认值的字段可以不填，去掉它的 default，让 openapi-typescript 按 required 生成成可选。
 * openapi-typescript 把有 default 的字段当作必有（defaultNonNullable），对响应是对的（服务端总会给出），对请求却错。
 * 请求与响应共用、而两边形状不同的类型，后端会分成 -Input / -Output 两个（如 StreamConfig），-Input 也只在请求里。
 */
export function relaxRequestDefaults(spec) {
  const schemas = spec.components.schemas
  const requests = new Set()
  for (const op of Object.values(spec.paths).flatMap((p) => Object.values(p))) refsIn(op.requestBody, () => false, requests)
  const inRequests = closure(schemas, requests)
  // 响应里的：接口里除请求体以外引用到的，和请求体引用不到的全部类型（如 SSE 事件）
  const outRoots = refsIn(spec.paths, (k) => k === 'requestBody')
  for (const name of Object.keys(schemas)) if (!inRequests.has(name)) outRoots.add(name)
  const inResponses = closure(schemas, outRoots)
  for (const name of inRequests) {
    if (inResponses.has(name)) continue
    for (const prop of Object.values(schemas[name].properties ?? {})) delete prop.default
  }
  return spec
}

/**
 * 响应里出现的类型中不是必有的字段（`类型.字段`）。服务端序列化时每个字段都会给出（没有值是 null），不必有的字段只来自
 * 请求与响应共用、带默认值的类型：前端读到它就得写兜底。这种类型要拆出一个字段全必有的 -Out 类型给响应用。
 */
export function optionalResponseFields(spec) {
  const schemas = spec.components.schemas
  const roots = refsIn(spec.paths, (k) => k === 'requestBody' || k === 'parameters')
  const found = []
  const visit = (node, at) => {
    if (Array.isArray(node)) return node.forEach((n) => visit(n, at))
    if (!node || typeof node !== 'object') return
    const required = new Set(node.required ?? [])
    for (const [name, prop] of Object.entries(node.properties ?? {})) {
      if (!required.has(name)) found.push(`${at}.${name}`)
      visit(prop, `${at}.${name}`)
    }
    for (const [k, v] of Object.entries(node)) if (k !== 'properties') visit(v, at)
  }
  for (const name of closure(schemas, roots)) visit(schemas[name], name)
  return found
}

/**
 * 后端各种编号的类型（platform::ids，名字以 Id 结尾、本身是字符串或整数）：生成成带品牌的字符串或数字，传错位置（把会话编号
 * 当 agent 编号、把 issue 号当触发器号）编译不过。新加的编号类型按名字自动带上，不用在这里登记。
 */
export function brandedKeyword(name, schema) {
  if (!name?.endsWith('Id')) return undefined
  if (schema?.type === 'string') return ts.SyntaxKind.StringKeyword
  if (schema?.type === 'integer') return ts.SyntaxKind.NumberKeyword
  return undefined
}

/** `string & { readonly __brand: '<name>' }`（整数编号是 number） */
function brand(name, keyword) {
  const f = ts.factory
  const tag = f.createPropertySignature(
    [f.createModifier(ts.SyntaxKind.ReadonlyKeyword)],
    '__brand',
    undefined,
    f.createLiteralTypeNode(f.createStringLiteral(name, true)),
  )
  return f.createIntersectionTypeNode([f.createKeywordTypeNode(keyword), f.createTypeLiteralNode([tag])])
}

/** 编号类型的写法（类型、格式、说明）→ 名字。后端泛型里的编号（如 `Change<Vec<IssueId>>`）被 utoipa 就地展开、没有引用，
 * 按与编号类型完全相同的写法认回来（说明就是那个类型的文档，各编号不同） */
export function brandedShapes(schemas) {
  const shapes = new Map()
  for (const [name, schema] of Object.entries(schemas)) {
    if (brandedKeyword(name, schema) !== undefined) shapes.set(shapeKey(schema), name)
  }
  return shapes
}

const shapeKey = (s) => JSON.stringify([s.type, s.format, s.description])

export async function generate() {
  const spec = JSON.parse(await readFile(SPEC, 'utf8'))
  const shapes = brandedShapes(spec.components.schemas)
  const transform = (schema, { path }) => {
    const own = path?.startsWith(PREFIX) ? path.slice(PREFIX.length) : ''
    const name = own !== '' && !own.includes('/') ? own : shapes.get(shapeKey(schema))
    const keyword = brandedKeyword(name, schema)
    return keyword === undefined ? undefined : brand(name, keyword)
  }
  // 没有字段的对象（如 ChangedEvent）默认生成 Record<string, never>，与 SSE 联合里加上的 type 字段相交就成了 never
  return HEADER + astToString(await openapiTS(relaxRequestDefaults(spec), { emptyObjectsUnknown: true, transform }))
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  await writeFile(OUTPUT, await generate())
}

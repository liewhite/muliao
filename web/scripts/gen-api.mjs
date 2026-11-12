// 从后端的接口描述（openapi.json，后端测试保证它与接口一致）生成前端类型 src/schema.d.ts；scripts/gen-api.test.mjs 保证
// 提交的生成结果与描述一致。用法：npm run gen:api
//
// openapi-typescript 7.13 的 peer 依赖写的是 TypeScript 5，项目用 6：package.json 的 overrides 让它用项目的
// TypeScript（它只用编译器 API 打印类型，实测生成正常）。它声明支持 TS 6 之后删掉这条 override。

import { readFile, writeFile } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'
import openapiTS, { astToString } from 'openapi-typescript'

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

export async function generate() {
  const spec = JSON.parse(await readFile(SPEC, 'utf8'))
  // 没有字段的对象（如 ChangedEvent）默认生成 Record<string, never>，与 SSE 联合里加上的 type 字段相交就成了 never
  return HEADER + astToString(await openapiTS(relaxRequestDefaults(spec), { emptyObjectsUnknown: true }))
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  await writeFile(OUTPUT, await generate())
}

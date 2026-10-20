// 从后端的接口描述（openapi.json，后端测试保证它与接口一致）生成前端类型 src/schema.d.ts；
// scripts/gen-api.test.mjs 保证提交的生成结果与描述一致。用法：npm run gen:api
//
// openapi-typescript 7.13 的 peer 依赖写的是 TypeScript 5，项目用 6：package.json 的 overrides 让它用项目的
// TypeScript（它只用编译器 API 打印类型，实测生成正常）。它声明支持 TS 6 之后删掉这条 override。

import { readFile, writeFile } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'
import openapiTS, { astToString } from 'openapi-typescript'

const SPEC = new URL('../openapi.json', import.meta.url)
export const OUTPUT = new URL('../src/schema.d.ts', import.meta.url)

const HEADER = '// 由 scripts/gen-api.mjs 从 openapi.json 生成，不要手改\n\n'

export async function generate() {
  const spec = JSON.parse(await readFile(SPEC, 'utf8'))
  return HEADER + astToString(await openapiTS(spec))
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  await writeFile(OUTPUT, await generate())
}

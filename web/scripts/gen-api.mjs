// 从后端的接口描述（openapi.json，后端测试保证它与接口一致）生成前端类型 src/schema.d.ts；
// scripts/gen-api.test.mjs 保证提交的生成结果与描述一致。用法：npm run gen:api

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

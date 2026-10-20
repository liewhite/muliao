import { readFile } from 'node:fs/promises'
import { expect, it } from 'vitest'
import { generate, OUTPUT } from './gen-api.mjs'

it('the committed frontend types match openapi.json', async () => {
  // 不一致时运行 npm run gen:api
  expect(await readFile(OUTPUT, 'utf8')).toBe(await generate())
})

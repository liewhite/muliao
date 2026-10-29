import { describe, expect, it } from 'vitest'
import { isBinary, joinPath, languageOf, MAX_PREVIEW_BYTES, previewKind } from './workspaceFiles'

describe('workspace files', () => {
  it('joins paths from the workspace root', () => {
    expect(joinPath('', 'evolve')).toBe('evolve')
    expect(joinPath('evolve', 'draft.md')).toBe('evolve/draft.md')
  })

  it('previews images whatever their size, other files only up to the limit', () => {
    expect(previewKind('chart.PNG', MAX_PREVIEW_BYTES * 10)).toBe('image')
    expect(previewKind('draft.md', 10)).toBe('markdown')
    expect(previewKind('main.py', 10)).toBe('text')
    expect(previewKind('Makefile', 10)).toBe('text')
    expect(previewKind('big.log', MAX_PREVIEW_BYTES + 1)).toBe('too-big')
    // svg 可能带脚本：按源码显示，不当图片
    expect(previewKind('icon.svg', 10)).toBe('text')
  })

  it('detects binary content by NUL', () => {
    expect(isBinary('abc')).toBe(false)
    expect(isBinary('a\u0000b')).toBe(true)
  })

  it('maps extensions and bare file names to highlight languages', () => {
    expect(languageOf('a.tsx')).toBe('typescript')
    expect(languageOf('config.toml')).toBe('ini')
    expect(languageOf('Makefile')).toBe('makefile')
    expect(languageOf('.env')).toBe(null)
    expect(languageOf('notes.txt')).toBe(null)
  })
})

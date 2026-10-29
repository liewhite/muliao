import { describe, expect, it } from 'vitest'
import { isBinary, joinPath, languageOf, MAX_PREVIEW_BYTES, previewKind, splitPath } from './workspaceFiles'

describe('workspace files', () => {
  it('joins paths from the workspace root', () => {
    expect(joinPath('', 'evolve')).toBe('evolve')
    expect(joinPath('evolve', 'draft.md')).toBe('evolve/draft.md')
    expect(splitPath('draft.md')).toEqual(['', 'draft.md'])
    expect(splitPath('a/b/draft.md')).toEqual(['a/b', 'draft.md'])
  })

  it('previews inline images whatever their size, other files only up to the limit', () => {
    const file = (name: string, size: number, inline = false) => ({ name, size, inline })
    expect(previewKind(file('chart.png', MAX_PREVIEW_BYTES * 10, true))).toBe('image')
    expect(previewKind(file('draft.md', 10))).toBe('markdown')
    expect(previewKind(file('main.py', 10))).toBe('text')
    expect(previewKind(file('Makefile', 10))).toBe('text')
    expect(previewKind(file('big.log', MAX_PREVIEW_BYTES + 1))).toBe('too-big')
    // svg 不是能内联的图片（可能带脚本）：按源码显示
    expect(previewKind(file('icon.svg', 10))).toBe('text')
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

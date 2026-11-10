import { describe, expect, it } from 'vitest'
import { oneOf, parseStringList } from './storedState'

describe('stored state parsers', () => {
  it('keeps a known option and falls back otherwise', () => {
    const parse = oneOf(['board', 'list'] as const, 'board')
    expect(parse('list')).toBe('list')
    expect(parse('graph')).toBe('board')
    expect(parse(null)).toBe('board')
  })

  it('reads a list of strings and drops anything else', () => {
    expect(parseStringList('["a","b"]')).toEqual(['a', 'b'])
    expect(parseStringList(null)).toEqual([])
    expect(parseStringList('{oops')).toEqual([])
    expect(parseStringList('[1]')).toEqual([])
  })
})

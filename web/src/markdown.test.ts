import { describe, expect, it } from 'vitest'
import { normalizeMath } from './markdown'

describe('normalizeMath', () => {
  it('turns a display formula on its own line into a block, keeping the list or quote around it', () => {
    expect(normalizeMath('- 项目\n  \\[\n  y=1\n  \\]\n- 下一项')).toBe('- 项目\n  $$\n  y=1\n  $$\n- 下一项')
    expect(normalizeMath('> \\[x^2\\]')).toBe('> $$\n> x^2\n> $$')
  })

  it('keeps an inline display formula inside its paragraph, list item and table cell', () => {
    expect(normalizeMath('1. 第一步：\\[ a+b \\]\n2. 第二步')).toBe('1. 第一步：$$a+b$$\n2. 第二步')
    expect(normalizeMath('> 引用 \\[x^2\\] 结束')).toBe('> 引用 $$x^2$$ 结束')
    expect(normalizeMath('| \\(x\\) | \\[y\\] |')).toBe('| $$x$$ | $$y$$ |')
  })

  it('leaves escaped brackets that are not math, such as citation numbers', () => {
    expect(normalizeMath('看 \\[1\\] 和 \\[2\\]')).toBe('看 \\[1\\] 和 \\[2\\]')
  })

  it('treats dollars as math only by the Pandoc rule, so prices stay text', () => {
    expect(normalizeMath('价格 $5 和 $10')).toBe('价格 \\$5 和 \\$10')
    expect(normalizeMath('$20,000 and $30,000')).toBe('\\$20,000 and \\$30,000')
    expect(normalizeMath('能量 $E=mc^2$。')).toBe('能量 $E=mc^2$。')
    expect(normalizeMath('已转义 \\$5')).toBe('已转义 \\$5')
  })

  it('keeps existing $$ blocks untouched, including \\\\[4pt] line spacing', () => {
    const block = '$$\n\\begin{aligned} a \\\\[4pt] b \\end{aligned}\n$$'
    expect(normalizeMath(block)).toBe(block)
  })

  it('never changes code: fences of any length or char, unclosed fences, inline code by backtick count', () => {
    const nested = '````md\n```\n\\(x\\) $5\n```\n````\n\\(y\\)'
    expect(normalizeMath(nested)).toBe('````md\n```\n\\(x\\) $5\n```\n````\n$$y$$')
    expect(normalizeMath('~~~\n\\(x\\)\n~~~')).toBe('~~~\n\\(x\\)\n~~~')
    expect(normalizeMath('```\n\\(x\\) streaming')).toBe('```\n\\(x\\) streaming')
    expect(normalizeMath('``a`\\(x\\)`` 和 \\(z\\)')).toBe('``a`\\(x\\)`` 和 $$z$$')
  })

  it('round-trips text without math exactly', () => {
    const text = '```py\nprint(1)\n```\n\n普通段落\n\n- a\n- b'
    expect(normalizeMath(text)).toBe(text)
  })
})

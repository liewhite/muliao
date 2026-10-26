import { renderToStaticMarkup } from 'react-dom/server'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it } from 'vitest'
import { Markdown } from './ui'

const html = (text: string) =>
  renderToStaticMarkup(
    <MemoryRouter>
      <Markdown text={text} />
    </MemoryRouter>,
  )
const count = (s: string, needle: string) => s.split(needle).length - 1

describe('Markdown', () => {
  it('renders a formula inside a numbered list without splitting the list', () => {
    const out = html('1. 第一步：\\[ a+b \\]\n2. 第二步')
    expect(count(out, '<ol')).toBe(1)
    expect(count(out, '<li')).toBe(2)
    expect(out).toContain('class="katex"')
  })

  it('renders a display formula inside a blockquote as a block', () => {
    const out = html('> 引用\n>\n> \\[x^2\\]')
    expect(count(out, '<blockquote')).toBe(1)
    expect(out).toContain('katex-display')
  })

  it('keeps prices as text', () => {
    const out = html('价格 $5 和 $10')
    expect(out).not.toContain('katex')
    expect(out).toContain('$5 和 $10')
  })

  it('shows only images from the file store; external images become links', () => {
    const out = html('![a](/api/files/x/a.png) ![t](https://example.com/t.png)')
    expect(out).toContain('<img src="/api/files/x/a.png"')
    expect(out).not.toContain('<img src="https://example.com')
    expect(out).toContain('href="https://example.com/t.png"')
  })

  it('opens only absolute links in a new tab and keeps footnote attributes', () => {
    const out = html('看注释[^1]\n\n[^1]: 注释内容\n\n[外部](https://example.com)')
    expect(out).toMatch(/<a href="https:\/\/example.com" target="_blank"/)
    expect(out).toMatch(/<a href="#user-content-fn-1"(?![^>]*target)[^>]*data-footnote-ref/)
  })

  it('links standalone issue refs to the issue page', () => {
    const out = html('见 #12，另见(#3)。\n\n#7 开头')
    expect(out).toMatch(/<a href="\/issues\/12"[^>]*>#12<\/a>/)
    expect(out).toMatch(/<a href="\/issues\/3"[^>]*>#3<\/a>/)
    expect(out).toMatch(/<a href="\/issues\/7"[^>]*>#7<\/a>/)
    expect(out).not.toContain('target=')
  })

  it('leaves code, external refs, entities, anchors and URLs alone', () => {
    const out = html(
      '`#1` owner/repo#2 &#123; #4a COIN-5 a#6 [锚](#7) https://example.com/x#8\n\n```\n#9\n```',
    )
    expect(out).not.toContain('/issues/')
    expect(out).toContain('<code>#1</code>')
    expect(out).toContain('owner/repo#2')
    expect(out).toContain('href="#7"')
    expect(out).toContain('href="https://example.com/x#8"')
  })

  it('routes in-app paths with their link attributes; backend paths stay plain links', () => {
    const out = html('[a](/issues/3 "提示") [b](/api/files/x.txt) [c](//evil.com)')
    expect(out).toMatch(/<a [^>]*href="\/issues\/3"[^>]*data-discover/)
    expect(out).toContain('title="提示"')
    expect(out).toContain('<a href="/api/files/x.txt">')
    expect(out).toContain('<a href="//evil.com">')
  })
})

/**
 * 把模型输出里的公式整理成 remark-math 能无歧义解析的形式。只在代码之外处理。
 *
 * remark-math 只认 $…$ 与 $$…$$，模型却常写 \(…\) 与 \[…\]；而且单个 $ 在普通文本里多半是价格。规则：
 * - 已有的 $$…$$ 原样保留。
 * - \[…\] 独占一行（前面可以有缩进或引用符号 >）时转成块级公式，并保留这个前缀，所在的列表、引用块不会被拆开；
 *   不独占一行时转成行内的 $$…$$（remark-math 把同一行内的 $$ 解析为行内公式，不打断段落、表格）。
 * - \(…\) 转成行内的 $$…$$。
 * - \[ 与 \( 也是 Markdown 合法的转义写法（如引用编号 \[1\]）：括号里不像公式（没有字母、\、^、_、=、{}）就不转换。
 * - 单个 $ 按 Pandoc 的规则判定是不是公式：开头的 $ 右边紧跟非空白，结尾的 $ 左边紧跟非空白、后面不是数字，
 *   所以「$20,000 和 $30,000」不是公式。不成对的 $ 一律转义成字面的 $。
 *
 * 代码按 CommonMark 识别：``` 或 ~~~ 开头的围栏代码块（任意长度，未闭合时到文末，流式输出中途也不改代码），
 * 以及按反引号个数配对的行内代码。
 */

const FENCE = /^ {0,3}(`{3,}|~{3,})/
const INLINE_CODE = /(`+)(?:(?!\1)[\s\S])*?\1(?!`)/g
const LOOKS_LIKE_MATH = /[A-Za-z\\^_={}]/
// 私用区字符作占位符：不会出现在正常文本里，也不会被下面的规则匹配
const HOLE = (i: number) => `\uE000${i}\uE001`
const HOLES = /\uE000(\d+)\uE001/g
const HAS_HOLE = /\uE000\d+\uE001/

interface Segment {
  code: boolean
  lines: string[]
}

/** 按行把文本切成围栏代码块与其余部分；每段至少一行，按顺序用换行连回去就是原文 */
function splitFences(text: string): Segment[] {
  const segments: Segment[] = []
  let current: Segment = { code: false, lines: [] }
  let fence: { char: string; length: number } | null = null
  const start = (code: boolean) => {
    if (current.lines.length > 0) segments.push(current)
    current = { code, lines: [] }
  }
  for (const line of text.split('\n')) {
    const marker = FENCE.exec(line)?.[1]
    if (fence === null && marker) {
      start(true)
      fence = { char: marker[0], length: marker.length }
      current.lines.push(line)
    } else if (fence !== null) {
      current.lines.push(line)
      if (marker && marker[0] === fence.char && marker.length >= fence.length && line.trim() === marker) {
        fence = null
        start(false)
      }
    } else {
      current.lines.push(line)
    }
  }
  start(false)
  return segments
}

function normalizeProse(text: string): string {
  const holes: string[] = []
  const keep = (s: string) => {
    holes.push(s)
    return HOLE(holes.length - 1)
  }
  const inline = (math: string) => keep(`$$${math.trim()}$$`)

  let out = text
    .replace(INLINE_CODE, keep)
    .replace(/\$\$[\s\S]+?\$\$/g, keep)
    // 独占一行的 \[ … \]（可跨行）：前缀是缩进与引用符号
    .replace(/^([ \t]*(?:>[ \t]?)*)\\\[([\s\S]+?)\\\][ \t]*$/gm, (whole, prefix: string, math: string) =>
      LOOKS_LIKE_MATH.test(math) ? keep(`${prefix}$$\n${prefix}${math.trim()}\n${prefix}$$`) : whole,
    )
    .replace(/\\\[([^\n]+?)\\\]/g, (whole, math: string) => (LOOKS_LIKE_MATH.test(math) ? inline(math) : whole))
    .replace(/\\\(([^\n]+?)\\\)/g, (whole, math: string) => (LOOKS_LIKE_MATH.test(math) ? inline(math) : whole))
    // Pandoc 规则下成对的 $…$
    .replace(/(?<![\\$])\$(?=[^\s$])((?:[^$\\\n]|\\.)+?)(?<=\S)\$(?![\d$])/g, keep)
  // 余下不成对的 $ 是字面的美元符号
  out = out.replace(/(?<!\\)\$/g, '\\$')
  // 占位符可能嵌套（块级公式里含已保留的片段），反复还原到没有为止
  while (HAS_HOLE.test(out)) out = out.replace(HOLES, (_, i: string) => holes[Number(i)])
  return out
}

export function normalizeMath(text: string): string {
  return splitFences(text)
    .map((s) => (s.code ? s.lines.join('\n') : normalizeProse(s.lines.join('\n'))))
    .join('\n')
}

// 工具调用详情按展示块渲染：每种块一个组件，样式只在这里定（块由后端按各工具的展示规则给出）。

import type { ViewBlock } from './api'
import { diffLineKind, toSegments, type TerminalBlock } from './toolViews'
import { CODE_BLOCK, Markdown } from './ui'

const TITLE = 'mb-0.5 break-all font-mono text-xs text-muted'

/** 终端框：深色、等宽；`$ 命令` 提示行、输出、状态行（失败时醒目） */
function Terminal({ blocks }: { blocks: TerminalBlock[] }) {
  return (
    <div className="my-1 max-h-96 overflow-auto rounded-lg bg-zinc-900 p-2.5 font-mono text-xs leading-relaxed text-zinc-100 dark:bg-black">
      {blocks.map((b, i) => (
        <div key={i}>
          {b.command && (
            <div className="whitespace-pre-wrap break-all">
              <span className="select-none text-emerald-400">$ </span>
              {b.command}
            </div>
          )}
          {b.output && <div className="whitespace-pre-wrap break-all text-zinc-300">{b.output}</div>}
          {b.status && <div className={b.failed ? 'text-red-400' : 'text-zinc-500'}>[{b.status}]</div>}
        </div>
      ))}
    </div>
  )
}

const DIFF_LINE = {
  add: 'bg-emerald-500/15 text-emerald-700 dark:text-emerald-300',
  del: 'bg-red-500/15 text-red-700 dark:text-red-300',
  hunk: 'text-muted',
  context: '',
}

function Block({ block }: { block: Exclude<ViewBlock, TerminalBlock> }) {
  switch (block.kind) {
    case 'code':
      return (
        <div>
          {block.title && <div className={TITLE}>{block.title}</div>}
          <pre className={CODE_BLOCK}>{block.text}</pre>
        </div>
      )
    case 'diff':
      return (
        <div>
          {block.title && <div className={TITLE}>{block.title}</div>}
          <pre className={CODE_BLOCK}>
            {block.text.split('\n').map((line, i) => (
              <div key={i} className={DIFF_LINE[diffLineKind(line)]}>
                {line || ' '}
              </div>
            ))}
          </pre>
        </div>
      )
    case 'markdown':
      return (
        <div className="my-1 rounded-lg border border-separator px-3 py-2 text-foreground">
          {block.title && <div className="mb-1 font-medium">{block.title}</div>}
          <Markdown text={block.text} />
        </div>
      )
    case 'fields':
      return block.rows.length === 0 ? null : (
        <dl className="my-1 grid grid-cols-[auto_1fr] gap-x-3 gap-y-0.5 text-xs">
          {block.rows.map((r, i) => (
            <div key={i} className="contents">
              <dt className="text-muted">{r.label}</dt>
              <dd className="break-all text-foreground">{r.value}</dd>
            </div>
          ))}
        </dl>
      )
    case 'table':
      return (
        <div className="my-1 max-h-80 overflow-auto">
          <table className="w-full text-left text-xs">
            <thead className="text-muted">
              <tr>
                {block.columns.map((c, i) => (
                  <th key={i} className="px-1.5 py-0.5 font-normal">
                    {c}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody className="text-foreground">
              {block.rows.map((row, i) => (
                <tr key={i} className="border-t border-separator">
                  {row.map((cell, j) => (
                    <td key={j} className="px-1.5 py-0.5 align-top">
                      {cell}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )
    case 'links':
      return (
        <ul className="my-1 flex flex-col gap-1.5 text-xs">
          {block.links.map((l, i) => (
            <li key={i}>
              <a href={l.url} target="_blank" rel="noreferrer" className="text-accent hover:underline">
                {l.title}
              </a>
              {l.title !== l.url && <div className="break-all text-muted">{l.url}</div>}
              {l.summary && <div className="line-clamp-2 text-foreground">{l.summary}</div>}
            </li>
          ))}
        </ul>
      )
  }
}

export function ToolViewBlocks({ blocks }: { blocks: ViewBlock[] }) {
  return (
    <>
      {toSegments(blocks).map((s, i) =>
        s.kind === 'terminal' ? <Terminal key={i} blocks={s.blocks} /> : <Block key={i} block={s.block} />,
      )}
    </>
  )
}

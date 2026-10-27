import { useState } from 'react'
import { Button, Card, Chip } from '@heroui/react'
import { Search } from 'lucide-react'
import { api, type AgentId, type HistoryEntry } from '../api'
import { EmptyHint, Loading, TextInput, useLoad } from '../ui'

const IMPORTANCE: Record<number, string> = { 2: '重要', 3: '很重要' }

/** 按日期分组（只用于不搜索时：接口按日期倒序给出；搜索结果按相关度，平铺列出） */
function byDay(entries: HistoryEntry[]): [string, HistoryEntry[]][] {
  const groups = new Map<string, HistoryEntry[]>()
  for (const e of entries) groups.set(e.day, [...(groups.get(e.day) ?? []), e])
  return [...groups]
}

/** agent 的工作历史（只读）：每日自进化时 agent 自己写，一件事一条，写了就不能改 */
export function HistoryList({ agent }: { agent: AgentId }) {
  const [input, setInput] = useState('')
  const [query, setQuery] = useState('')
  const history = useLoad(() => api.agentHistory(agent, query || undefined), [agent, query])

  return (
    <div className="flex flex-col gap-3">
      <p className="text-sm text-muted">
        agent 每天自进化时总结前一天做过的事，只追加、不能改；agent 用 history_search 查。
      </p>
      <form
        className="flex items-end gap-2"
        onSubmit={(e) => {
          e.preventDefault()
          setQuery(input.trim())
        }}
      >
        <div className="flex-1">
          <TextInput label="关键词（空格分隔，全部出现）" value={input} onChange={setInput} />
        </div>
        <Button type="submit" variant="secondary">
          <Search className="size-4" />
          搜索
        </Button>
      </form>
      <Loading state={history}>
        {(list) =>
          list.length === 0 ? (
            <EmptyHint>{query ? '没有匹配的记录' : '还没有历史记录'}</EmptyHint>
          ) : (
            <div className="flex flex-col gap-4">
              {query ? (
                <div className="flex flex-col gap-2">
                  {list.map((e) => (
                    <EntryCard key={e.id} entry={e} showDay />
                  ))}
                </div>
              ) : (
                byDay(list).map(([day, entries]) => (
                  <section key={day} className="flex flex-col gap-2">
                    <h2 className="text-sm font-medium text-muted">{day}</h2>
                    {entries.map((e) => (
                      <EntryCard key={e.id} entry={e} />
                    ))}
                  </section>
                ))
              )}
            </div>
          )
        }
      </Loading>
    </div>
  )
}

function EntryCard({ entry: e, showDay }: { entry: HistoryEntry; showDay?: boolean }) {
  return (
    <Card>
      <Card.Content className="flex flex-col gap-1 text-sm">
        <div className="flex flex-wrap items-center gap-2">
          {showDay && <span className="text-muted">{e.day}</span>}
          <span className="font-medium">{e.title}</span>
          {IMPORTANCE[e.importance] && (
            <Chip size="sm" color={e.importance === 3 ? 'danger' : 'warning'}>
              {IMPORTANCE[e.importance]}
            </Chip>
          )}
        </div>
        <p className="whitespace-pre-wrap text-muted">{e.summary}</p>
        {e.refs.length > 0 && (
          <div className="flex flex-wrap gap-1">
            {e.refs.map((r, i) => (
              <code key={i} className="rounded bg-surface-secondary px-1.5 py-0.5 font-mono text-xs">
                {r}
              </code>
            ))}
          </div>
        )}
      </Card.Content>
    </Card>
  )
}

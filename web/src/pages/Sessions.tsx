import { useEffect, useState } from 'react'
import { Card, Chip, SearchField, Spinner } from '@heroui/react'
import { ChevronDown, ChevronRight, Hourglass } from 'lucide-react'
import { Link, useMatch } from 'react-router-dom'
import { api, type AgentId, type SessionListItem, sessionRoute } from '../api'
import { useRefetch } from '../events'
import { FILTERS, ORIGIN_LABEL, groupByAgent, parseFilter, splitActive, type Filter } from '../sessionGroups'
import {
  EmptyHint,
  Loading,
  Page,
  Segmented,
  agentLabel,
  relativeTime,
  useAgents,
  useLoad,
} from '../ui'

// 选中的分类跨刷新、跨页面记住：从会话或 issue 回来时停在原来的分类（不放 URL：宽屏分屏时右侧切会话会换掉 URL）
const FILTER_KEY = 'muliao.sessions.filter'

// 折叠起来的 agent 分组，跨刷新记住
const COLLAPSED_KEY = 'muliao.sessions.collapsed'

function loadCollapsed(): Set<string> {
  try {
    const raw = localStorage.getItem(COLLAPSED_KEY)
    return new Set(raw ? (JSON.parse(raw) as string[]) : [])
  } catch {
    return new Set()
  }
}

// 在运行的会话：卡片左侧描强调色边、标题前转圈、带「运行中」标签；在等待的（后台任务、委派、用户回答）：
// 浅色边、沙漏、「等待中」。置顶区里不在 agent 分组下，带上 agent 名
const STATE_CARD: Record<SessionListItem['state'], string> = {
  running: 'border-l-4 border-accent bg-accent/5',
  waiting: 'border-l-4 border-accent/40',
  idle: '',
}

function SessionCard({ s, agentName }: { s: SessionListItem; agentName?: string }) {
  // 宽屏分屏时右侧正打开的会话
  const open = useMatch(sessionRoute(s.id)) !== null
  return (
    <Link to={sessionRoute(s.id)}>
      <Card className={`transition-colors hover:bg-surface-hover ${STATE_CARD[s.state]} ${open ? 'ring-2 ring-accent' : ''}`}>
        <Card.Content className="flex flex-row items-center gap-3">
          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-2 font-medium">
              {s.state === 'running' && <Spinner size="sm" aria-label="运行中" />}
              {s.state === 'waiting' && <Hourglass className="size-4 shrink-0 text-muted" aria-label="等待中" />}
              {s.unread && (
                <>
                  <span className="size-2 shrink-0 rounded-full bg-accent" aria-hidden />
                  <span className="sr-only">未读</span>
                </>
              )}
              <span className="truncate">{s.title}</span>
            </div>
            <div className="mt-1 flex gap-1.5">
              {agentName !== undefined && (
                <Chip size="sm" variant="secondary">
                  {agentName}
                </Chip>
              )}
              <Chip size="sm" variant="secondary">
                {ORIGIN_LABEL[s.origin]}
              </Chip>
              {s.state === 'running' && (
                <Chip size="sm" color="accent" variant="soft">
                  运行中
                </Chip>
              )}
              {s.state === 'waiting' && (
                <Chip size="sm" variant="soft">
                  等待中
                </Chip>
              )}
            </div>
          </div>
          <span className="shrink-0 text-xs text-muted">{relativeTime(s.updated_at)}</span>
        </Card.Content>
      </Card>
    </Link>
  )
}

export default function Sessions() {
  const [filter, setFilterState] = useState(() => parseFilter(localStorage.getItem(FILTER_KEY)))
  const setFilter = (f: Filter) => {
    localStorage.setItem(FILTER_KEY, f)
    setFilterState(f)
  }
  const agents = useAgents()
  const [collapsed, setCollapsed] = useState(loadCollapsed)
  const toggle = (id: AgentId) =>
    setCollapsed((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      localStorage.setItem(COLLAPSED_KEY, JSON.stringify([...next]))
      return next
    })
  // 关键字输入停下 300ms 后才查（搜消息全文），空白等于不搜
  const [text, setText] = useState('')
  const [keyword, setKeyword] = useState('')
  useEffect(() => {
    const timer = setTimeout(() => setKeyword(text.trim()), 300)
    return () => clearTimeout(timer)
  }, [text])
  const sessions = useLoad(
    () =>
      api.sessions({
        origin: FILTERS[filter].origins ?? undefined,
        q: keyword === '' ? undefined : keyword,
        limit: 200,
      }),
    [filter, keyword],
  )
  useRefetch('sessions', sessions.reload)

  return (
    <Page title="会话">
      <Segmented
        label="会话分类"
        value={filter}
        onChange={setFilter}
        options={Object.entries(FILTERS).map(([id, f]) => ({
          id: id as Filter,
          label: f.label,
        }))}
      />
      <SearchField value={text} onChange={setText} aria-label="搜索会话" fullWidth variant="secondary">
        <SearchField.Group>
          <SearchField.SearchIcon />
          <SearchField.Input placeholder="搜索标题或消息内容" />
          <SearchField.ClearButton />
        </SearchField.Group>
      </SearchField>
      <Loading state={sessions}>
        {(list) => {
          const { active, rest } = splitActive(list)
          return list.length === 0 ? (
            <EmptyHint>{keyword === '' ? '没有会话' : '没有匹配的会话'}</EmptyHint>
          ) : (
            <div className="flex flex-col gap-4">
              {active.length > 0 && (
                <section className="flex flex-col gap-2">
                  <div className="flex items-center gap-2 text-sm font-semibold text-accent">
                    <Spinner size="sm" color="current" />
                    <span>进行中</span>
                    <span className="font-normal">{active.length}</span>
                  </div>
                  {active.map((s) => (
                    <SessionCard key={s.id} s={s} agentName={agentLabel(agents.data, s.agent_id)} />
                  ))}
                </section>
              )}
              {groupByAgent(rest).map((g) => {
                // 搜索时一律展开：命中的会话不被折叠藏起来
                const open = keyword !== '' || !collapsed.has(g.agentId)
                return (
                  <section key={g.agentId} className="flex flex-col gap-2">
                    <button
                      type="button"
                      className="flex items-center gap-2 text-left text-sm font-semibold text-muted hover:text-foreground"
                      aria-expanded={open}
                      disabled={keyword !== ''}
                      onClick={() => toggle(g.agentId)}
                    >
                      {open ? <ChevronDown className="size-4" /> : <ChevronRight className="size-4" />}
                      <span>{agentLabel(agents.data, g.agentId)}</span>
                      <span className="font-normal">{g.sessions.length}</span>
                      {g.unread > 0 && (
                        <Chip size="sm" color="accent" variant="soft">
                          {g.unread} 未读
                        </Chip>
                      )}
                    </button>
                    {open && g.sessions.map((s) => <SessionCard key={s.id} s={s} />)}
                  </section>
                )
              })}
            </div>
          )
        }}
      </Loading>
    </Page>
  )
}

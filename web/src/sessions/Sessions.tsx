import { useEffect, useState } from 'react'
import { Button, Card, Chip, SearchField, Spinner } from '@heroui/react'
import { ChevronDown, ChevronRight, Hourglass } from 'lucide-react'
import { Link, useMatch } from 'react-router-dom'
import { api, type AgentId, type SessionListItem } from '../api'
import { agentModel, agentLabel } from '../agents/agentGroups'
import { oneOf, parseStringList, useStoredState } from '../storedState'
import { FILTERS, ORIGIN_LABEL, groupByAgent, splitActive, type Filter } from './sessionGroups'
import { EmptyHint, Loading, Page, Segmented, relativeTime } from '../ui'
import { useLoad } from '../load'
import { sessionRoute } from '../routes'
import { useAgents } from '../agents/agentList'

// 选中的分类跨刷新、跨页面记住：从会话或 issue 回来时停在原来的分类（不放 URL：宽屏分屏时右侧切会话会换掉 URL）
const FILTER_KEY = 'muliao.sessions.filter'
// 没记过或不认识的值时为「对话」
const parseFilter = oneOf(Object.keys(FILTERS) as Filter[], 'chat')

// 折叠起来的 agent 分组，跨刷新记住
const COLLAPSED_KEY = 'muliao.sessions.collapsed'

// 一页的会话数
const PAGE = 200

// 在运行的会话：卡片左侧描强调色边、标题前转圈、带「运行中」标签；在等待的（后台任务、委派、用户回答）：
// 浅色边、沙漏、「等待中」。置顶区里不在 agent 分组下，带上 agent 名和它现在配置的模型
const STATE_CARD: Record<SessionListItem['state'], string> = {
  running: 'border-l-4 border-accent bg-accent/5',
  waiting: 'border-l-4 border-accent/40',
  idle: '',
}

function SessionCard({ s, agentName, model }: { s: SessionListItem; agentName?: string; model?: string }) {
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
            <div className="mt-1 flex flex-wrap gap-1.5">
              {agentName !== undefined && (
                <Chip size="sm" variant="secondary">
                  {agentName}
                </Chip>
              )}
              {model !== undefined && (
                <Chip size="sm" variant="secondary">
                  {model}
                </Chip>
              )}
              <Chip size="sm" variant="secondary">
                {ORIGIN_LABEL[s.origin.kind]}
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
  const [filter, setFilter] = useStoredState(FILTER_KEY, parseFilter)
  const agents = useAgents()
  const [collapsedList, setCollapsed] = useStoredState(COLLAPSED_KEY, parseStringList, JSON.stringify)
  const collapsed = new Set(collapsedList)
  const toggle = (id: AgentId) =>
    setCollapsed(collapsed.has(id) ? collapsedList.filter((x) => x !== id) : [...collapsedList, id])
  // 关键字输入停下 300ms 后才查（搜消息全文），空白等于不搜
  const [text, setText] = useState('')
  const [keyword, setKeyword] = useState('')
  useEffect(() => {
    const timer = setTimeout(() => setKeyword(text.trim()), 300)
    return () => clearTimeout(timer)
  }, [text])
  // 一次多取一页：列表满了就在底下给「显示更多」；换分类、换关键字时回到第一页（记下是给哪个筛选加的页）
  const listKey = `${filter}\n${keyword}`
  const [more, setMore] = useState({ key: listKey, limit: PAGE })
  const limit = more.key === listKey ? more.limit : PAGE
  const sessions = useLoad(() =>
      api.sessions({
        origin: FILTERS[filter].origins ?? undefined,
        q: keyword === '' ? undefined : keyword,
        limit,
      }),
    [filter, keyword, limit],
  )

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
                    <SessionCard
                      key={s.id}
                      s={s}
                      agentName={agentLabel(agents.data, s.agent_id)}
                      model={agentModel(agents.data, s.agent_id)}
                    />
                  ))}
                </section>
              )}
              {groupByAgent(rest).map((g) => {
                // 搜索时一律展开：命中的会话不被折叠藏起来
                const open = keyword !== '' || !collapsed.has(g.agentId)
                const model = agentModel(agents.data, g.agentId)
                return (
                  <section key={g.agentId} className="flex flex-col gap-2">
                    <button
                      type="button"
                      className="flex items-center gap-2 text-left text-sm font-semibold text-muted hover:text-foreground"
                      aria-expanded={open}
                      disabled={keyword !== ''}
                      onClick={() => toggle(g.agentId)}
                    >
                      {open ? <ChevronDown className="size-4 shrink-0" /> : <ChevronRight className="size-4 shrink-0" />}
                      <span className="shrink-0">{agentLabel(agents.data, g.agentId)}</span>
                      <span className="font-normal">{g.sessions.length}</span>
                      {model !== undefined && <span className="min-w-0 truncate text-xs font-normal">{model}</span>}
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
              {list.length === limit && (
                <Button variant="secondary" onPress={() => setMore({ key: listKey, limit: limit + PAGE })}>
                  显示更多
                </Button>
              )}
            </div>
          )
        }}
      </Loading>
    </Page>
  )
}

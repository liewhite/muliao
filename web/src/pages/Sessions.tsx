import { useState } from 'react'
import { Card, Chip, Spinner } from '@heroui/react'
import { ChevronDown, ChevronRight, Hourglass, Plus } from 'lucide-react'
import { Link, useMatch } from 'react-router-dom'
import { api, asAgentId, type AgentId, type Origin, type SessionListItem, sessionRoute } from '../api'
import { shouldRefetch, useServerEvents } from '../events'
import { groupByAgent, splitActive } from '../sessionGroups'
import {
  EmptyHint,
  LinkButton,
  Loading,
  Page,
  Segmented,
  SelectInput,
  agentLabel,
  relativeTime,
  useAgents,
  useLoad,
} from '../ui'

const ORIGIN_LABEL: Record<Origin, string> = {
  user: '对话',
  trigger: '触发',
  delegation: '委派',
  issue: 'Issue',
}

// 触发器（定时、外部事件）每次触发、issue 唤醒 agent 都会产生会话，默认不显示以免淹没收件箱
const FILTERS = {
  inbox: { label: '收件箱', origins: ['user', 'delegation'] as Origin[] },
  auto: { label: '自动', origins: ['trigger', 'issue'] as Origin[] },
  all: { label: '全部', origins: undefined },
}
type Filter = keyof typeof FILTERS

// 「所有 agent」选项的 id；查询时映射为不过滤
const ALL_AGENTS = '@all'

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
  const [filter, setFilter] = useState<Filter>('inbox')
  const [agent, setAgent] = useState(ALL_AGENTS)
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
  const sessions = useLoad(
    () =>
      api.sessions({
        origin: FILTERS[filter].origins,
        agent_id: agent === ALL_AGENTS ? undefined : asAgentId(agent),
        limit: 200,
      }),
    [filter, agent],
  )
  useServerEvents((e) => {
    if (shouldRefetch(e)) void sessions.reload()
  })

  return (
    <Page
      title="会话"
      actions={
        <LinkButton to="/new">
          <Plus className="size-4" />
          新对话
        </LinkButton>
      }
    >
      <div className="flex flex-wrap items-end gap-3">
        <Segmented
          label="会话筛选"
          value={filter}
          onChange={setFilter}
          options={Object.entries(FILTERS).map(([id, f]) => ({
            id: id as Filter,
            label: f.label,
          }))}
        />
        <div className="min-w-40 flex-1">
          <SelectInput
            label="Agent"
            value={agent}
            onChange={setAgent}
            options={[
              { id: ALL_AGENTS, label: '所有 agent' },
              ...(agents.data ?? []).map((a) => ({ id: a.id, label: a.name })),
            ]}
          />
        </div>
      </div>
      <Loading state={sessions}>
        {(list) => {
          const { active, rest } = splitActive(list)
          return list.length === 0 ? (
            <EmptyHint>没有会话</EmptyHint>
          ) : agent !== ALL_AGENTS ? (
            <div className="flex flex-col gap-2">
              {list.map((s) => (
                <SessionCard key={s.id} s={s} />
              ))}
            </div>
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
                const open = !collapsed.has(g.agentId)
                return (
                  <section key={g.agentId} className="flex flex-col gap-2">
                    <button
                      type="button"
                      className="flex items-center gap-2 text-left text-sm font-semibold text-muted hover:text-foreground"
                      aria-expanded={open}
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

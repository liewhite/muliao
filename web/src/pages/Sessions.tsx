import { useState } from 'react'
import { Card, Chip } from '@heroui/react'
import { Plus } from 'lucide-react'
import { Link } from 'react-router-dom'
import { api, asAgentId, type Origin, sessionRoute } from '../api'
import { useServerEvents } from '../events'
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
}

// 触发器（定时、外部事件）每次触发都会产生会话，默认不显示以免淹没收件箱
const FILTERS = {
  inbox: { label: '收件箱', origins: ['user', 'delegation'] as Origin[] },
  auto: { label: '自动', origins: ['trigger'] as Origin[] },
  all: { label: '全部', origins: undefined },
}
type Filter = keyof typeof FILTERS

// 「所有 agent」选项的 id；查询时映射为不过滤
const ALL_AGENTS = '@all'

export default function Sessions() {
  const [filter, setFilter] = useState<Filter>('inbox')
  const [agent, setAgent] = useState(ALL_AGENTS)
  const agents = useAgents()
  const sessions = useLoad(
    () => api.sessions({ origin: FILTERS[filter].origins, agent_id: agent === ALL_AGENTS ? undefined : asAgentId(agent), limit: 100 }),
    [filter, agent],
  )
  useServerEvents((e) => {
    if (e.type === 'reconnected' || (e.type === 'task' && e.kind === 'run')) void sessions.reload()
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
          options={Object.entries(FILTERS).map(([id, f]) => ({ id: id as Filter, label: f.label }))}
        />
        <div className="min-w-40 flex-1">
          <SelectInput
            label="Agent"
            value={agent}
            onChange={setAgent}
            options={[{ id: ALL_AGENTS, label: '所有 agent' }, ...(agents.data ?? []).map((a) => ({ id: a.id, label: a.name }))]}
          />
        </div>
      </div>
      <Loading state={sessions}>
        {(list) =>
          list.length === 0 ? (
            <EmptyHint>没有会话</EmptyHint>
          ) : (
            <div className="flex flex-col gap-2">
              {list.map((s) => (
                <Link key={s.id} to={sessionRoute(s.id)}>
                  <Card className="transition-colors hover:bg-surface-hover">
                    <Card.Content className="flex flex-row items-center gap-3">
                      <div className="min-w-0 flex-1">
                        <div className="flex items-center gap-2 font-medium">
                          {s.unread && (
                            <>
                              <span className="size-2 shrink-0 rounded-full bg-accent" aria-hidden />
                              <span className="sr-only">未读</span>
                            </>
                          )}
                          <span className="truncate">{s.title}</span>
                        </div>
                        <div className="mt-1 flex gap-1.5">
                          <Chip size="sm" color="accent" variant="soft">
                            {agentLabel(agents.data, s.agent_id)}
                          </Chip>
                          <Chip size="sm" variant="secondary">
                            {ORIGIN_LABEL[s.origin]}
                          </Chip>
                        </div>
                      </div>
                      <span className="shrink-0 text-xs text-muted">{relativeTime(s.updated_at)}</span>
                    </Card.Content>
                  </Card>
                </Link>
              ))}
            </div>
          )
        }
      </Loading>
    </Page>
  )
}

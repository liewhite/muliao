import { useState } from 'react'
import { Card } from '@heroui/react'
import { api, type Usage } from '../api'
import { EmptyHint, Loading, Page, Segmented, agentLabel, useAgents, useLoad } from '../ui'
import { cacheHitPercent, formatTokens as tokens, windowOf } from '../usage'

const WINDOWS = {
  '7': { label: '近 7 天', days: 7 },
  '30': { label: '近 30 天', days: 30 },
} as const
type WindowKey = keyof typeof WINDOWS

const EMPTY: Usage = {
  agent_id: null,
  model: null,
  day: null,
  requests: 0,
  input_tokens: 0,
  output_tokens: 0,
  cache_read_tokens: 0,
  cache_write_tokens: 0,
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex flex-col">
      <span className="text-[11px] text-muted">{label}</span>
      <span className="font-mono text-sm tabular-nums">{value}</span>
    </div>
  )
}

/** 一个分组的四项 token 与请求数。输入是总数，缓存读写是其中的两部分，不相加 */
function Row({ label, usage }: { label: string; usage: Usage }) {
  return (
    <div className="flex flex-col gap-1.5 px-4 py-3">
      <div className="flex items-baseline justify-between gap-2">
        <span className="min-w-0 truncate font-medium">{label}</span>
        <span className="shrink-0 text-xs text-muted">{usage.requests} 次请求</span>
      </div>
      <div className="grid grid-cols-4 gap-2">
        <Stat label="输入" value={tokens(usage.input_tokens)} />
        <Stat label="输出" value={tokens(usage.output_tokens)} />
        <Stat label="缓存读" value={tokens(usage.cache_read_tokens)} />
        <Stat label="缓存写" value={tokens(usage.cache_write_tokens)} />
      </div>
    </div>
  )
}

function Section({ title, rows }: { title: string; rows: { key: string; label: string; usage: Usage }[] }) {
  return (
    <Card className="p-0">
      <div className="border-b border-separator px-4 py-2 text-sm font-medium text-muted">{title}</div>
      {rows.length === 0 ? (
        <EmptyHint>这段时间没有用量</EmptyHint>
      ) : (
        <div className="flex flex-col divide-y divide-separator">
          {rows.map((r) => (
            <Row key={r.key} label={r.label} usage={r.usage} />
          ))}
        </div>
      )}
    </Card>
  )
}

/** 按天的输入 token 条形图：条长按这段时间里的最大值归一 */
function Daily({ days }: { days: Usage[] }) {
  const peak = Math.max(1, ...days.map((d) => d.input_tokens))
  return (
    <Card className="p-0">
      <div className="border-b border-separator px-4 py-2 text-sm font-medium text-muted">
        按天（输入 token，深色部分为缓存命中）
      </div>
      {days.length === 0 ? (
        <EmptyHint>这段时间没有用量</EmptyHint>
      ) : (
        <div className="flex flex-col gap-2 px-4 py-3">
          {days.map((d) => (
            <div key={d.day} className="flex items-center gap-2">
              <span className="w-12 shrink-0 font-mono text-[11px] tabular-nums text-muted">
                {d.day?.slice(5)}
              </span>
              <div className="h-4 min-w-0 flex-1 overflow-hidden rounded bg-surface-secondary">
                <div className="h-full bg-accent-soft" style={{ width: `${(d.input_tokens / peak) * 100}%` }}>
                  <div
                    className="h-full bg-accent"
                    style={{ width: `${cacheHitPercent(d.input_tokens, d.cache_read_tokens)}%` }}
                  />
                </div>
              </div>
              <span className="w-12 shrink-0 text-right font-mono text-[11px] tabular-nums">
                {tokens(d.input_tokens)}
              </span>
            </div>
          ))}
        </div>
      )}
    </Card>
  )
}

export default function UsagePage() {
  const [window_, setWindow] = useState<WindowKey>('7')
  const agents = useAgents()
  const usage = useLoad(async () => {
    const [since, until] = windowOf(WINDOWS[window_].days)
    const [total, byAgent, byModel, byDay] = await Promise.all([
      api.usage(since, until, []),
      api.usage(since, until, ['agent']),
      api.usage(since, until, ['model']),
      api.usage(since, until, ['day']),
    ])
    return { total: total[0] ?? EMPTY, byAgent, byModel, byDay }
  }, [window_])

  return (
    <Page title="用量" back="/settings">
      <Segmented
        label="统计区间"
        value={window_}
        onChange={setWindow}
        options={Object.entries(WINDOWS).map(([id, w]) => ({ id: id as WindowKey, label: w.label }))}
      />
      <Loading state={usage}>
        {({ total, byAgent, byModel, byDay }) => (
          <div className="flex flex-col gap-3">
            <Card className="p-0">
              <Row label="合计" usage={total} />
              <div className="border-t border-separator px-4 py-2 text-xs text-muted">
                输入 token 已包含缓存读写；这段时间缓存命中{' '}
                {cacheHitPercent(total.input_tokens, total.cache_read_tokens)}%
              </div>
            </Card>
            <Section
              title="按 agent"
              rows={byAgent.map((u) => ({
                key: u.agent_id ?? '',
                // 已删除的 agent 查不到名称，显示它的 id：账目不随 agent 删除而消失
                label: u.agent_id === null ? '未知' : agentLabel(agents.data, u.agent_id),
                usage: u,
              }))}
            />
            <Section
              title="按模型"
              rows={byModel.map((u) => ({ key: u.model ?? '', label: u.model ?? '未知', usage: u }))}
            />
            <Daily days={byDay} />
          </div>
        )}
      </Loading>
    </Page>
  )
}

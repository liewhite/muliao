import { useState } from 'react'
import { Button, Card, Chip, Disclosure, toast } from '@heroui/react'
import { History, RotateCcw } from 'lucide-react'
import { USER, api, type AgentId, type RevisionTarget } from './api'
import {
  EmptyHint,
  ErrorAlert,
  Loading,
  Markdown,
  agentLabel,
  formatTime,
  useAction,
  useAgents,
  useConfirm,
  useLoad,
} from './ui'

/**
 * 记忆或条目的版本历史（新的在前，第一条即当前生效），可以把旧版恢复为新的一版（secret 的版本不含值，只能看）。
 * 展开时才加载（条目页一屏有很多条目，不为每个都请求一次）；展开着时 current 变化就重新加载
 */
export function ResourceHistory({
  target,
  current,
  onRestored,
}: {
  target: RevisionTarget
  current: string
  onRestored: () => void
}) {
  const [open, setOpen] = useState(false)
  return (
    <Disclosure className="rounded-xl border border-separator px-3 py-2" isExpanded={open} onExpandedChange={setOpen}>
      <Disclosure.Heading>
        <Disclosure.Trigger className="flex w-full items-center gap-2 text-sm text-muted">
          <History className="size-4" />
          历史版本
          <Disclosure.Indicator />
        </Disclosure.Trigger>
      </Disclosure.Heading>
      <Disclosure.Content>
        <Disclosure.Body className="flex flex-col gap-2 pt-2">
          {open && <RevisionList target={target} current={current} onRestored={onRestored} />}
        </Disclosure.Body>
      </Disclosure.Content>
    </Disclosure>
  )
}

function RevisionList({
  target,
  current,
  onRestored,
}: {
  target: RevisionTarget
  current: string
  onRestored: () => void
}) {
  const key = JSON.stringify(target)
  const history = useLoad(() => api.history(target), [key, current])
  const agents = useAgents()
  const confirm = useConfirm()
  const action = useAction()

  async function restore(rev: number, at: string) {
    const ok = await confirm({
      title: `恢复 ${formatTime(at)} 的版本？`,
      body: '它会作为新的一版生效，现有历史保留。',
      confirmLabel: '恢复',
    })
    if (ok && (await action.run(() => api.restore(target, rev)))) {
      toast.success('已恢复')
      onRestored()
    }
  }

  return (
    <>
      <ErrorAlert error={action.error} />
      <Loading state={history}>
        {(list) =>
          list.length === 0 ? (
            <EmptyHint>还没有历史</EmptyHint>
          ) : (
            list.map((r, i) => (
              <Card key={r.rev} variant="secondary">
                <Card.Header className="flex flex-row items-center gap-2">
                  <Card.Description className="flex-1">
                    {formatTime(r.created_at)} ·{' '}
                    {r.actor === USER ? '用户' : agentLabel(agents.data, r.actor as AgentId)}
                  </Card.Description>
                  {r.content === null && (
                    <Chip size="sm" color="danger" variant="soft">
                      删除
                    </Chip>
                  )}
                  {i === 0 ? (
                    <Chip size="sm" color="success" variant="soft">
                      当前
                    </Chip>
                  ) : (
                    r.restorable && (
                      <Button
                        size="sm"
                        variant="ghost"
                        isDisabled={action.busy}
                        onPress={() => void restore(r.rev, r.created_at)}
                      >
                        <RotateCcw className="size-4" />
                        恢复
                      </Button>
                    )
                  )}
                </Card.Header>
                <Card.Content className="max-h-60 overflow-auto text-sm">
                  {!r.content?.trim() ? (
                    <span className="text-muted">（空）</span>
                  ) : target.kind === 'memory' ? (
                    <Markdown text={r.content} />
                  ) : (
                    <pre className="font-mono text-xs whitespace-pre-wrap break-all">
                      {JSON.stringify(JSON.parse(r.content), null, 2)}
                    </pre>
                  )}
                </Card.Content>
              </Card>
            ))
          )
        }
      </Loading>
    </>
  )
}

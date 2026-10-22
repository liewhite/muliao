import { useEffect, useState } from 'react'
import { Button, Card, Chip, Disclosure, toast } from '@heroui/react'
import { History, RotateCcw } from 'lucide-react'
import { USER, api, type AgentId, type ResourceKind } from '../api'
import {
  ActionButton,
  EmptyHint,
  ErrorAlert,
  Loading,
  Markdown,
  Page,
  TextAreaInput,
  agentLabel,
  formatTime,
  useAction,
  useAgents,
  useConfirm,
  useLoad,
} from '../ui'

/** 文本资源的版本历史（新的在前，第一条即当前生效），可以把旧版恢复为新的一版。current 变化时重新加载 */
export function ResourceHistory({
  kind,
  owner,
  current,
  onRestored,
}: {
  kind: ResourceKind
  owner: AgentId | null
  current: string
  onRestored: () => void
}) {
  const history = useLoad(() => api.history(kind, owner), [kind, owner, current])
  const agents = useAgents()
  const confirm = useConfirm()
  const action = useAction()

  async function restore(rev: string, at: string) {
    const ok = await confirm({ title: `恢复 ${formatTime(at)} 的版本？`, body: '它会作为新的一版生效，现有历史保留。', confirmLabel: '恢复' })
    if (ok && (await action.run(() => api.restore(kind, owner, rev)))) {
      toast.success('已恢复')
      onRestored()
    }
  }

  return (
    <Disclosure className="rounded-xl border border-separator px-3 py-2">
      <Disclosure.Heading>
        <Disclosure.Trigger className="flex w-full items-center gap-2 text-sm text-muted">
          <History className="size-4" />
          历史版本
          <Disclosure.Indicator />
        </Disclosure.Trigger>
      </Disclosure.Heading>
      <Disclosure.Content>
        <Disclosure.Body className="flex flex-col gap-2 pt-2">
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
                        {formatTime(r.created_at)} · {r.author === USER ? '用户' : agentLabel(agents.data, r.author as AgentId)}
                      </Card.Description>
                      {i === 0 ? (
                        <Chip size="sm" color="success" variant="soft">
                          当前
                        </Chip>
                      ) : (
                        <Button size="sm" variant="ghost" isDisabled={action.busy} onPress={() => void restore(r.rev, r.created_at)}>
                          <RotateCcw className="size-4" />
                          恢复
                        </Button>
                      )}
                    </Card.Header>
                    <Card.Content className="max-h-60 overflow-auto text-sm">
                      {r.content.trim() ? <Markdown text={r.content} /> : <span className="text-muted">（空）</span>}
                    </Card.Content>
                  </Card>
                ))
              )
            }
          </Loading>
        </Disclosure.Body>
      </Disclosure.Content>
    </Disclosure>
  )
}

/** 一份记忆（owner 为 null 即全局记忆）：编辑、保存，以及它的历史版本 */
export function MemoryEditor({ owner, hint }: { owner: AgentId | null; hint: string }) {
  const memory = useLoad(() => api.memory(owner), [owner])
  const [content, setContent] = useState('')
  const action = useAction()
  useEffect(() => {
    if (memory.data) setContent(memory.data.content)
  }, [memory.data])

  return (
    <Loading state={memory}>
      {(m) => (
        <div className="flex flex-col gap-3">
          <TextAreaInput
            label={`记忆（${content.length}/${m.max_chars} 字符）`}
            hint={hint}
            value={content}
            onChange={setContent}
            rows={14}
            mono
          />
          <ErrorAlert error={action.error} />
          <ActionButton
            isDisabled={content === m.content}
            isPending={action.busy}
            onPress={() =>
              void action.run(async () => {
                await api.setMemory(owner, content)
                toast.success('记忆已保存')
                await memory.reload()
              })
            }
          >
            保存记忆
          </ActionButton>
          <ResourceHistory kind="memory" owner={owner} current={m.content} onRestored={() => void memory.reload()} />
        </div>
      )}
    </Loading>
  )
}

/** 全局记忆：所有 agent 共享，放在各自的记忆之前；各 agent 的自进化也会修改它 */
export default function GlobalMemory() {
  return (
    <Page title="全局记忆">
      <MemoryEditor owner={null} hint="所有 agent 每次运行都会看到，放在各自的记忆之前；agent 也会用工具修改它。每次保存都会留下一个版本" />
    </Page>
  )
}

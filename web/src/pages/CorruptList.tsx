import { Button, Card } from '@heroui/react'
import { Trash2 } from 'lucide-react'
import { api, type AgentId, type CorruptResource } from '../api'
import { ErrorAlert, useAction, useConfirm, useLoad } from '../ui'

const KINDS = {
  skill: { label: 'skill', load: api.corruptSkills, remove: api.deleteSkill },
  mcp: { label: 'MCP server', load: api.corruptMcp, remove: api.deleteMcp },
} as const

/** owner 名下读不出来的 skill 或 MCP 配置（多半是绕过工具改坏的）：agent 用不了它们，这里列出原因，可以删除。 */
export function CorruptList({ kind, owner }: { kind: keyof typeof KINDS; owner: AgentId | null }) {
  const { label, load, remove } = KINDS[kind]
  const items = useLoad(() => load(owner), [load, owner])
  const confirm = useConfirm()
  const action = useAction()

  async function del(item: CorruptResource) {
    const ok = await confirm({ title: `删除损坏的 ${label}「${item.name}」？`, confirmLabel: '删除', danger: true })
    if (ok) await action.run(() => remove(owner, item.name).then(() => items.reload()))
  }

  if (!items.data || items.data.length === 0) return <ErrorAlert error={items.error} />
  return (
    <div className="flex flex-col gap-2">
      <ErrorAlert error={action.error} />
      {items.data.map((item) => (
        <Card key={item.name} className="border border-danger">
          <Card.Header className="flex flex-row items-start gap-3">
            <div className="min-w-0 flex-1">
              <Card.Title className="font-mono text-base">{item.name}</Card.Title>
              <Card.Description className="break-all text-danger">
                已损坏，agent 用不了：{item.problem}
              </Card.Description>
            </div>
            <Button size="sm" variant="danger-soft" isDisabled={action.busy} onPress={() => void del(item)}>
              <Trash2 className="size-4" />
              删除
            </Button>
          </Card.Header>
        </Card>
      ))}
    </div>
  )
}

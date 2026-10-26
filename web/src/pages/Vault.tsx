import { useState } from 'react'
import { Button, Card, toast } from '@heroui/react'
import { Trash2 } from 'lucide-react'
import { api, type AgentId, type CatalogItem } from '../api'
import { ItemAccess, OwnerSelect } from './itemParts'
import { ResourceHistory } from './Memory'
import { ActionButton, CollapsibleForm, EmptyHint, ErrorAlert, TextInput, formatTime, useAction, useConfirm } from '../ui'

function SecretForm({ onSaved }: { onSaved: () => void }) {
  const [owner, setOwner] = useState<AgentId | null>(null)
  const [key, setKey] = useState('')
  const [value, setValue] = useState('')
  const [description, setDescription] = useState('')
  const action = useAction()

  async function save() {
    const ok = await action.run(async () => {
      await api.setSecret(owner, key, value, description)
      toast.success(`已保存 ${key}`)
    })
    if (ok) {
      setKey('')
      setValue('')
      setDescription('')
      onSaved()
    }
  }

  return (
    <CollapsibleForm title="新建或改值">
      <TextInput label="Key" hint="已有的同名 secret 改值与说明，归属不变" value={key} onChange={setKey} placeholder="SLACK_TOKEN" mono isRequired />
      <TextInput label="值" type="password" value={value} onChange={setValue} isRequired />
      <TextInput label="说明" value={description} onChange={setDescription} />
      <OwnerSelect value={owner} onChange={setOwner} />
      <ErrorAlert error={action.error} />
      <ActionButton isDisabled={!key || !value} isPending={action.busy} onPress={() => void save()}>
        保存
      </ActionButton>
    </CollapsibleForm>
  )
}

function SecretItem({ item, onChange }: { item: CatalogItem; onChange: () => void }) {
  // 只有内置工具没有修改时间
  const updatedAt = item.updated_at
  if (updatedAt === null) throw new Error(`secret ${item.name} has no updated_at`)
  const confirm = useConfirm()
  const action = useAction()

  async function remove() {
    const ok = await confirm({
      title: `删除 secret「${item.name}」？`,
      body: '引用它的 MCP 与命令会失败。',
      confirmLabel: '删除',
      danger: true,
    })
    if (ok) await action.run(() => api.deleteSecret(item.name).then(onChange))
  }

  return (
    <Card>
      <Card.Content className="flex flex-col gap-2">
        <div className="flex flex-row items-center gap-3">
          <div className="min-w-0 flex-1">
            <div className="font-mono font-medium">{item.name}</div>
            <div className="truncate text-xs text-muted">
              {item.description || '无说明'} · {formatTime(updatedAt)}
            </div>
          </div>
          <Button isIconOnly size="sm" variant="ghost" aria-label={`删除 ${item.name}`} onPress={() => void remove()}>
            <Trash2 className="size-4" />
          </Button>
        </div>
        <ItemAccess item={item} />
        <ErrorAlert error={action.error} />
        <ResourceHistory kind="secret" target={{ name: item.name }} current={updatedAt} onRestored={onChange} />
      </Card.Content>
    </Card>
  )
}

/** secret 的条目页：值加密保存、不再显示；items 是条目池里（已筛选的）secret */
export function SecretTab({ items, onChange }: { items: CatalogItem[]; onChange: () => void }) {
  return (
    <div className="flex flex-col gap-3">
      <p className="text-sm text-muted">
        agent 通过 key 引用：MCP 配置里写 <code className="font-mono">{'${vault:KEY}'}</code>，shell 里用{' '}
        <code className="font-mono">run_shell(secrets=[KEY])</code> 注入同名环境变量。版本历史只记说明，不含值。
      </p>
      <SecretForm onSaved={onChange} />
      {items.length === 0 ? (
        <EmptyHint>没有 secret</EmptyHint>
      ) : (
        <div className="flex flex-col gap-2">
          {items.map((i) => (
            <SecretItem key={i.name} item={i} onChange={onChange} />
          ))}
        </div>
      )}
    </div>
  )
}

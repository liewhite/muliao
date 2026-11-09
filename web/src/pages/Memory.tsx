import { useEffect, useState } from 'react'
import { Button, Card, toast } from '@heroui/react'
import { ChevronDown, ChevronRight, Trash2 } from 'lucide-react'
import { Link } from 'react-router-dom'
import { api, type AgentId, type ItemOf } from '../api'
import { ItemAccess, OwnerSelect } from './itemParts'
import {
  ActionButton,
  CollapsibleForm,
  EmptyHint,
  ErrorAlert,
  Loading,
  TextAreaInput,
  TextInput,
  formatTime,
  useAction,
  useConfirm,
  useLoad,
} from '../ui'

/** 全局记忆的名字（后端 agents/permissions.py 的 GLOBAL_MEMORY）：base 固定收录，不能删 */
export const GLOBAL_MEMORY = 'global'

/** 一块记忆：编辑正文、保存（写的是它的文件） */
export function MemoryEditor({ name, hint, onSaved }: { name: string; hint: string; onSaved?: () => void }) {
  const memory = useLoad(() => api.memory(name), [name])
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
            label={`记忆 ${m.name}（${content.length}/${m.max_chars} 字符）`}
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
                await api.saveMemory(name, { content })
                toast.success('记忆已保存')
                await memory.reload()
                onSaved?.()
              })
            }
          >
            保存记忆
          </ActionButton>
          <div className="font-mono text-xs break-all text-muted">{m.path}</div>
        </div>
      )}
    </Loading>
  )
}

function MemoryForm({ onSaved }: { onSaved: () => void }) {
  const [owner, setOwner] = useState<AgentId | null>(null)
  const [name, setName] = useState('')
  const action = useAction()

  async function save() {
    const ok = await action.run(async () => {
      await api.saveMemory(name, { owner, content: '' })
      toast.success(`已新建记忆 ${name}`)
    })
    if (ok) {
      setName('')
      onSaved()
    }
  }

  return (
    <CollapsibleForm title="新建共享记忆">
      <TextInput label="名字" hint="小写字母、数字、- 和 _" value={name} onChange={setName} placeholder="trading" mono isRequired />
      <OwnerSelect value={owner} onChange={setOwner} />
      <ErrorAlert error={action.error} />
      <ActionButton isDisabled={!name} isPending={action.busy} onPress={() => void save()}>
        新建
      </ActionButton>
    </CollapsibleForm>
  )
}

function MemoryItem({ item, onChange }: { item: ItemOf<'memory'>; onChange: () => void }) {
  const [open, setOpen] = useState(false)
  const confirm = useConfirm()
  const action = useAction()

  async function remove() {
    const ok = await confirm({
      title: `删除记忆「${item.name}」？`,
      body: '用它的 agent 从下一次运行起就看不到这些内容了。',
      confirmLabel: '删除',
      danger: true,
    })
    if (ok) await action.run(() => api.deleteMemory(item.name).then(onChange))
  }

  return (
    <Card>
      <Card.Content className="flex flex-col gap-2">
        <div className="flex flex-row items-center gap-3">
          <Button isIconOnly size="sm" variant="ghost" aria-label={open ? '收起' : '编辑'} onPress={() => setOpen(!open)}>
            {open ? <ChevronDown className="size-4" /> : <ChevronRight className="size-4" />}
          </Button>
          <div className="min-w-0 flex-1">
            <div className="font-mono font-medium">{item.name}</div>
            <div className="truncate text-xs text-muted">
              <span className="font-mono">{item.path}</span> · {formatTime(item.updated_at)}
            </div>
          </div>
          {item.name !== GLOBAL_MEMORY && (
            <Button isIconOnly size="sm" variant="ghost" aria-label={`删除 ${item.name}`} onPress={() => void remove()}>
              <Trash2 className="size-4" />
            </Button>
          )}
        </div>
        <ItemAccess item={item} onTransferred={onChange} />
        <ErrorAlert error={action.error} />
        {open && (
          <MemoryEditor
            name={item.name}
            hint="能用它的 agent 每次运行都会看到全文；能改它的 agent 也会直接改它的文件"
            onSaved={onChange}
          />
        )}
      </Card.Content>
    </Card>
  )
}

/** 记忆的资产页：全局记忆（base 收录）、共享记忆（角色写 `*:memory:<名>` 收录，成员能用也能改）与各 agent 的私有记忆 */
export function MemoryTab({ items, onChange }: { items: ItemOf<'memory'>[]; onChange: () => void }) {
  return (
    <div className="flex flex-col gap-3">
      <p className="text-sm text-muted">
        agent 能用（use）的每一块记忆每次运行都全文放进指令，能改（write）的直接改它的文件。私有记忆归它的 agent；共享记忆在角色里写{' '}
        <code className="font-mono">*:memory:名字</code> 分给一组 agent（成员能用也能改），只读写{' '}
        <code className="font-mono">use:memory:名字</code>。
      </p>
      <MemoryForm onSaved={onChange} />
      {items.length === 0 ? (
        <EmptyHint>没有记忆</EmptyHint>
      ) : (
        <div className="flex flex-col gap-2">
          {items.map((i) => (
            <MemoryItem key={i.name} item={i} onChange={onChange} />
          ))}
        </div>
      )}
    </div>
  )
}

/** agent 页的记忆：它名下的记忆块（私有记忆）逐块编辑；全局与共享记忆在资产页 */
export function AgentMemories({ agent }: { agent: AgentId }) {
  const items = useLoad(() => api.items(), [])
  return (
    <Loading state={items}>
      {(all) => {
        const own = all.filter((i): i is ItemOf<'memory'> => i.kind === 'memory' && i.owner === agent)
        return (
          <div className="flex flex-col gap-4">
            {own.length === 0 ? (
              <EmptyHint>它名下没有记忆</EmptyHint>
            ) : (
              own.map((m) => (
                <MemoryEditor
                  key={m.name}
                  name={m.name}
                  hint="agent 每次运行都会看到它能用的全部记忆，也会自行修改"
                  onSaved={() => void items.reload()}
                />
              ))
            )}
            <p className="text-sm text-muted">
              它还能用全局记忆与角色分给它的共享记忆：见 <Link to="/items?kind=memory" className="text-accent">AI 资产 · 记忆</Link>。
            </p>
          </div>
        )
      }}
    </Loading>
  )
}

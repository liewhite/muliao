import { useState } from 'react'
import { Button, Card, toast } from '@heroui/react'
import { ChevronDown, ChevronRight, Trash2 } from 'lucide-react'
import { Link } from 'react-router-dom'
import { GLOBAL_MEMORY, api, type AgentId, type AssetOf, type Memory } from '../api'
import { useDraft } from '../drafts'
import { AssetAccess } from './assetParts'
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
} from '../ui'
import { useLoad } from '../load'

/** 一块记忆是哪一块：平台的按名字（owner 为 null），agent 的私有记忆按它所属的 agent（name 只是显示用的 agent 名） */
type MemoryRef = { name: string; owner: AgentId | null }

/** 一块记忆：编辑正文、保存（写的是它的文件）。agent 随时会改它：随 changed 重新拉取，保存带打开编辑时的版本 */
export function MemoryEditor({ memory: ref, hint }: { memory: MemoryRef; hint: string }) {
  const { name, owner } = ref
  const memory = useLoad(() => (owner === null ? api.memory(name) : api.agentMemory(owner)), [name, owner])
  return <Loading state={memory}>{(m) => <MemoryDraft latest={m} hint={hint} />}</Loading>
}

function saveMemory(m: Memory, content: string): Promise<Memory> {
  const body = { content, version: m.version }
  return m.owner === null ? api.saveMemory(m.name, body) : api.saveAgentMemory(m.owner, body)
}

function MemoryDraft({ latest, hint }: { latest: Memory; hint: string }) {
  // 版本在草稿里：改过之后不再跟服务端，交回的就是开始编辑时的版本；期间被别处改过就 409（assets.check_version）
  const doc = useDraft(latest)
  const action = useAction()
  const { content } = doc.draft
  const edited = Object.keys(doc.changes).length > 0
  return (
    <div className="flex flex-col gap-3">
      <TextAreaInput
        label={`记忆 ${latest.name}（${content.length}/${latest.max_chars} 字符）`}
        hint={hint}
        value={content}
        onChange={(content) => doc.set({ content })}
        rows={14}
        mono
      />
      <ErrorAlert error={action.error} />
      <div className="flex gap-2">
        <ActionButton
          isDisabled={!edited}
          isPending={action.busy}
          onPress={() =>
            void action.run(async () => {
              const saved = await saveMemory(doc.draft, content)
              toast.success('记忆已保存')
              // 存下的是规整过空白的：以它为新的起点，随 changed 重新拉取到这个版本时就对上了
              doc.saved(saved)
            })
          }
        >
          保存记忆
        </ActionButton>
        {edited && (
          <Button
            variant="ghost"
            onPress={() => {
              doc.discard()
              action.setError(null)
            }}
          >
            放弃修改
          </Button>
        )}
      </div>
      <div className="font-mono text-xs break-all text-muted">{latest.path}</div>
    </div>
  )
}

/** 新建平台的共享记忆（私有记忆建 agent 时就有，每个 agent 一块） */
function MemoryForm() {
  const [name, setName] = useState('')
  const action = useAction()

  async function save() {
    const ok = await action.run(async () => {
      await api.saveMemory(name, { content: '', version: null })
      toast.success(`已新建记忆 ${name}`)
    })
    if (ok) setName('')
  }

  return (
    <CollapsibleForm title="新建共享记忆">
      <TextInput label="名字" hint="小写字母、数字、- 和 _" value={name} onChange={setName} placeholder="trading" mono isRequired />
      <ErrorAlert error={action.error} />
      <ActionButton isDisabled={!name} isPending={action.busy} onPress={() => void save()}>
        新建
      </ActionButton>
    </CollapsibleForm>
  )
}

function MemoryItem({ item }: { item: AssetOf<'memory'> }) {
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
    if (ok) await action.run(() => api.deleteAsset('memory', item.name))
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
              <span className="font-mono">{item.path}</span>
              {item.updated_at && <> · {formatTime(item.updated_at)}</>}
            </div>
          </div>
          {item.owner === null && item.name !== GLOBAL_MEMORY && (
            <Button isIconOnly size="sm" variant="ghost" aria-label={`删除 ${item.name}`} onPress={() => void remove()}>
              <Trash2 className="size-4" />
            </Button>
          )}
        </div>
        <AssetAccess asset={item} />
        <ErrorAlert error={action.error} />
        {open && (
          <MemoryEditor
            memory={item}
            hint="能用它的 agent 每次运行都会看到全文；能改它的 agent 也会直接改它的文件"
          />
        )}
      </Card.Content>
    </Card>
  )
}

/** 记忆的资产页：全局记忆（base 收录）、共享记忆（角色写 `*:memory:<名>` 收录，成员能用也能改）与各 agent 的私有记忆 */
export function MemoryTab({ assets }: { assets: AssetOf<'memory'>[] }) {
  return (
    <div className="flex flex-col gap-3">
      <p className="text-sm text-muted">
        agent 能用（use）的每一块记忆每次运行都全文放进指令，能改（write）的直接改它的文件。私有记忆只属于它的 agent（规则不按名字指它）；共享记忆在角色里写{' '}
        <code className="font-mono">*:memory:名字</code> 分给一组 agent（成员能用也能改），只读写{' '}
        <code className="font-mono">use:memory:名字</code>。
      </p>
      <MemoryForm />
      {assets.length === 0 ? (
        <EmptyHint>没有记忆</EmptyHint>
      ) : (
        <div className="flex flex-col gap-2">
          {assets.map((i) => (
            <MemoryItem key={i.path} item={i} />
          ))}
        </div>
      )}
    </div>
  )
}

/** agent 页的记忆：它名下的记忆块（私有记忆）逐块编辑；全局与共享记忆在资产页 */
export function AgentMemories({ agent }: { agent: AgentId }) {
  const assets = useLoad(() => api.assets(), [])
  return (
    <Loading state={assets}>
      {(all) => {
        const own = all.filter((i): i is AssetOf<'memory'> => i.kind === 'memory' && i.owner === agent)
        return (
          <div className="flex flex-col gap-4">
            {own.length === 0 ? (
              <EmptyHint>它名下没有记忆</EmptyHint>
            ) : (
              own.map((m) => (
                <MemoryEditor
                  key={m.path}
                  memory={m}
                  hint="agent 每次运行都会看到它能用的全部记忆，也会自行修改"
                />
              ))
            )}
            <p className="text-sm text-muted">
              它还能用全局记忆与角色分给它的共享记忆：见 <Link to="/ai-assets?kind=memory" className="text-accent">AI 资产 · 记忆</Link>。
            </p>
          </div>
        )
      }}
    </Loading>
  )
}

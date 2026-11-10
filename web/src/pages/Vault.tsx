import { useState } from 'react'
import { Button, Card, Chip, toast } from '@heroui/react'
import { Trash2 } from 'lucide-react'
import { api, type AgentId, type AssetOf } from '../api'
import { AssetAccess, OwnerSelect } from './assetParts'
import {
  ActionButton,
  CollapsibleForm,
  EmptyHint,
  ErrorAlert,
  TextInput,
  ToggleSwitch,
  formatTime,
  useAction,
  useConfirm,
} from '../ui'

function SecretForm({ assets, onSaved }: { assets: AssetOf<'secret'>[]; onSaved: () => void }) {
  const [owner, setOwner] = useState<AgentId | null>(null)
  const [key, setKey] = useState('')
  const [value, setValue] = useState('')
  const [description, setDescription] = useState('')
  // 没动过开关为 null：已有的保持原值、新建的允许（只改值时不把不允许的悄悄放开）
  const [allowShell, setAllowShell] = useState<boolean | null>(null)
  const existing = assets.find((i) => i.name === key)
  const action = useAction()

  async function save() {
    const ok = await action.run(async () => {
      await api.setSecret(owner, key, value, description, allowShell)
      toast.success(`已保存 ${key}`)
    })
    if (ok) {
      setKey('')
      setValue('')
      setDescription('')
      setAllowShell(null)
      onSaved()
    }
  }

  return (
    <CollapsibleForm title="新建或改值">
      <TextInput label="Key" hint="已有的同名 secret 改值与说明，归属不变" value={key} onChange={setKey} placeholder="SLACK_TOKEN" mono isRequired />
      <TextInput label="值" type="password" value={value} onChange={setValue} isRequired />
      <TextInput label="说明" value={description} onChange={setDescription} />
      <div className="flex flex-row items-center gap-2 text-sm">
        <ToggleSwitch
          label="允许 shell 使用"
          isSelected={allowShell ?? existing?.allow_shell ?? true}
          onChange={setAllowShell}
        />
        <span>允许 shell 使用</span>
        <span className="text-xs text-muted">关掉后只能经 skill 脚本（skill_run）、MCP 配置和内置工具用</span>
      </div>
      <OwnerSelect value={owner} onChange={setOwner} />
      <ErrorAlert error={action.error} />
      <ActionButton isDisabled={!key || !value} isPending={action.busy} onPress={() => void save()}>
        保存
      </ActionButton>
    </CollapsibleForm>
  )
}

function SecretItem({ item, onChange }: { item: AssetOf<'secret'>; onChange: () => void }) {
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
            <div className="flex flex-row items-center gap-2">
              <span className="font-mono font-medium">{item.name}</span>
              {!item.allow_shell && (
                <Chip size="sm" variant="soft">
                  仅 skill 脚本
                </Chip>
              )}
            </div>
            <div className="truncate text-xs text-muted">
              {item.description || '无说明'} · {formatTime(item.updated_at)}
            </div>
          </div>
          <Button isIconOnly size="sm" variant="ghost" aria-label={`删除 ${item.name}`} onPress={() => void remove()}>
            <Trash2 className="size-4" />
          </Button>
        </div>
        <AssetAccess asset={item} onTransferred={onChange} />
        <ErrorAlert error={action.error} />
      </Card.Content>
    </Card>
  )
}

/** secret 的资产页：值加密保存、不再显示；assets 是资产池里（已筛选的）secret */
export function SecretTab({ assets, onChange }: { assets: AssetOf<'secret'>[]; onChange: () => void }) {
  return (
    <div className="flex flex-col gap-3">
      <p className="text-sm text-muted">
        agent 通过 key 引用：MCP 配置里写 <code className="font-mono">{'${vault:KEY}'}</code>，shell 里用{' '}
        <code className="font-mono">run_shell(secrets=[KEY])</code> 注入同名环境变量；不允许 shell 的只能用{' '}
        <code className="font-mono">skill_run(secrets=[KEY])</code> 经 skill 里的脚本注入。
      </p>
      <SecretForm assets={assets} onSaved={onChange} />
      {assets.length === 0 ? (
        <EmptyHint>没有 secret</EmptyHint>
      ) : (
        <div className="flex flex-col gap-2">
          {assets.map((i) => (
            <SecretItem key={i.name} item={i} onChange={onChange} />
          ))}
        </div>
      )}
    </div>
  )
}

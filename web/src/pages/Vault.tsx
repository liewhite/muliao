import { useState } from 'react'
import { Button, Card, Chip, toast } from '@heroui/react'
import { Trash2 } from 'lucide-react'
import { api, type AgentId, type SecretInfo } from '../api'
import { splitView } from '../ownership'
import { ActionButton,
  EmptyHint,
  ErrorAlert,
  InheritedSection,
  Loading,
  Page,
  TextInput,
  formatTime,
  useAction,
  useConfirm,
  useLoad,
} from '../ui'

/** 继承来的公共 secret：只能看，管理入口在设置页。 */
function InheritedSecretItem({ item }: { item: SecretInfo }) {
  return (
    <Card>
      <Card.Content className="flex flex-row items-center gap-3">
        <div className="min-w-0 flex-1">
          <div className="font-mono font-medium">{item.key}</div>
          <div className="truncate text-xs text-muted">
            {item.description || '无说明'} · {formatTime(item.updated_at)}
          </div>
        </div>
        {item.shadowed && (
          <Chip size="sm" color="warning" variant="soft">
            已被私有项覆盖
          </Chip>
        )}
      </Card.Content>
    </Card>
  )
}

/** owner 为 null 时管理公共 secret；否则管理该 agent 的私有 secret，并只读列出它继承的公共 secret。 */
export function SecretManager({ owner }: { owner: AgentId | null }) {
  const confirm = useConfirm()
  const secrets = useLoad(() => api.secrets(owner), [owner])
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
      await secrets.reload()
    }
  }

  async function remove(s: SecretInfo) {
    const ok = await confirm({
      title: `删除 secret「${s.key}」？`,
      body:
        owner === null
          ? '引用它的 MCP 与命令会失败。'
          : '引用它的 MCP 与命令会改用同名的公共 secret；没有同名的公共 secret 就会失败。',
      confirmLabel: '删除',
      danger: true,
    })
    if (ok) await action.run(() => api.deleteSecret(s.owner, s.key).then(secrets.reload))
  }

  return (
    <div className="flex flex-col gap-3">
      <Card>
        <Card.Content className="flex flex-col gap-4">
          <TextInput label="Key" value={key} onChange={setKey} placeholder="SLACK_TOKEN" mono isRequired />
          <TextInput label="值" type="password" value={value} onChange={setValue} isRequired />
          <TextInput label="说明" value={description} onChange={setDescription} />
          <ErrorAlert error={action.error} />
          <ActionButton isDisabled={!key || !value} isPending={action.busy} onPress={() => void save()}>
            保存（同名覆盖）
          </ActionButton>
        </Card.Content>
      </Card>
      <Loading state={secrets}>
        {(all) => {
          const { own, inherited } = splitView(all, owner)
          return (
            <div className="flex flex-col gap-4">
              {own.length === 0 ? (
                <EmptyHint>还没有 secret</EmptyHint>
              ) : (
                <div className="flex flex-col gap-2">
                  {own.map((s) => (
                    <Card key={s.key}>
                      <Card.Content className="flex flex-row items-center gap-3">
                        <div className="min-w-0 flex-1">
                          <div className="font-mono font-medium">{s.key}</div>
                          <div className="truncate text-xs text-muted">
                            {s.description || '无说明'} · {formatTime(s.updated_at)}
                          </div>
                        </div>
                        <Button isIconOnly size="sm" variant="ghost" aria-label={`删除 ${s.key}`} onPress={() => void remove(s)}>
                          <Trash2 className="size-4" />
                        </Button>
                      </Card.Content>
                    </Card>
                  ))}
                </div>
              )}
              {inherited.length > 0 && (
                <InheritedSection title="继承的公共 Secrets" to="/vault">
                  {inherited.map((s) => (
                    <InheritedSecretItem key={s.key} item={s} />
                  ))}
                </InheritedSection>
              )}
            </div>
          )
        }}
      </Loading>
    </div>
  )
}

export default function Vault() {
  return (
    <Page title="公共 Vault">
      <p className="text-sm text-muted">
        Secret 加密保存，值不会再显示。agent 通过 key 引用：MCP 配置里写{' '}
        <code className="font-mono">{'${vault:KEY}'}</code>，shell 里用{' '}
        <code className="font-mono">run_shell(secrets=[KEY])</code> 注入同名环境变量。公共 secret 所有 agent 都能用，也是公共 MCP
        server 唯一能引用的；agent 私有的 secret 在该 agent 详情页的 Secrets 标签里管理，同名时遮蔽公共的。
      </p>
      <SecretManager owner={null} />
    </Page>
  )
}

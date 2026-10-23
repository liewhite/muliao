import { useState } from 'react'
import { Button, Card, Chip, Disclosure } from '@heroui/react'
import { Pencil, Trash2 } from 'lucide-react'
import { api, type ResolvedTools, type Role, type RoleInput, type ToolPermission } from '../api'
import {
  ActionButton,
  CollapsibleForm,
  EmptyHint,
  ErrorAlert,
  Loading,
  Page,
  TextInput,
  ToggleSwitch,
  useAction,
  useConfirm,
  useLoad,
} from '../ui'

const ACCESS: Record<ToolPermission['access'], { label: string; color: 'default' | 'warning' }> = {
  read: { label: '读', color: 'default' },
  write: { label: '写', color: 'warning' },
}

/** 通配符列表的输入：空格或逗号分隔 */
const parsePatterns = (text: string) => text.split(/[\s,]+/).filter(Boolean)

/** 按规则最终能用的工具（MCP 不归权限管，不在其中）：每个标读 / 写 */
export function ToolList({ resolved }: { resolved: ResolvedTools }) {
  return (
    <div className="flex flex-col gap-1 text-sm">
      {resolved.tools.map((t) => (
        <div key={t.tool} className="flex flex-wrap items-center gap-2">
          <Chip size="sm" variant="soft" color={ACCESS[t.access].color}>
            {ACCESS[t.access].label}
          </Chip>
          <span className="font-mono">{t.tool}</span>
          <span className="font-mono text-xs text-muted">{t.id}</span>
          {t.tool === 'run_temp_agent' && resolved.temp_agent && <TempAgentTools tools={resolved.temp_agent} />}
        </div>
      ))}
      {resolved.tools.length === 0 && <span className="text-muted">没有工具</span>}
    </div>
  )
}

/** 临时 agent 按同一份规则能用的工具（调用方工具的子集），另有调用方的 MCP */
function TempAgentTools({ tools }: { tools: string[] }) {
  return (
    <span className="w-full ps-10 text-xs text-muted">
      临时 agent 能用：{tools.length ? <span className="font-mono">{tools.join('、')}</span> : '没有内置工具'}，另有调用方的 MCP
    </span>
  )
}

/** 可展开的「最终能用的工具（N）」 */
export function ToolsDisclosure({ resolved, title = '最终能用的工具' }: { resolved: ResolvedTools; title?: string }) {
  return (
    <Disclosure>
      <Disclosure.Heading>
        <Disclosure.Trigger className="flex items-center gap-1 text-sm text-muted">
          <Disclosure.Indicator className="ms-0" />
          {title}（{resolved.tools.length}）
        </Disclosure.Trigger>
      </Disclosure.Heading>
      <Disclosure.Content>
        <Disclosure.Body className="pt-2">
          <ToolList resolved={resolved} />
        </Disclosure.Body>
      </Disclosure.Content>
    </Disclosure>
  )
}

export type PermissionValue = { roles: string[]; allow: string[]; deny: string[] }

/** agent 的工具权限：角色开关、额外的 allow / deny 通配符，以及按它们算出的最终工具（服务端计算） */
export function PermissionsInput({ value, onChange }: { value: PermissionValue; onChange: (v: PermissionValue) => void }) {
  const roles = useLoad(() => api.roles(), [])
  const [allowText, setAllowText] = useState(value.allow.join(' '))
  const [denyText, setDenyText] = useState(value.deny.join(' '))
  const key = JSON.stringify(value)
  const resolved = useLoad(() => api.resolvePermissions(value), [key])
  return (
    <div className="flex flex-col gap-2">
      <div className="text-sm font-medium">工具权限</div>
      <div className="text-xs text-muted">admin 是全部工具；agent 是除写全局资源和管理 agent 以外的全部工具</div>
      <Loading state={roles}>
        {(list) => {
          const names = list.map((r) => r.name)
          return (
            <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
              {list.map((r) => (
                <label key={r.name} className="flex items-center gap-2 text-sm">
                  <ToggleSwitch
                    label={r.name}
                    isSelected={value.roles.includes(r.name)}
                    onChange={(on) =>
                      onChange({
                        ...value,
                        roles: on
                          ? names.filter((n) => n === r.name || value.roles.includes(n))
                          : value.roles.filter((n) => n !== r.name),
                      })
                    }
                  />
                  <span className="font-mono">{r.name}</span>
                  <span className="truncate text-xs text-muted">{r.description}</span>
                </label>
              ))}
            </div>
          )
        }}
      </Loading>
      <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
        <TextInput
          label="额外允许"
          hint="通配符，空格分隔，如 issue:*"
          mono
          value={allowText}
          onChange={(t) => {
            setAllowText(t)
            onChange({ ...value, allow: parsePatterns(t) })
          }}
        />
        <TextInput
          label="禁止"
          hint="优先于所有允许，如 trigger:*"
          mono
          value={denyText}
          onChange={(t) => {
            setDenyText(t)
            onChange({ ...value, deny: parsePatterns(t) })
          }}
        />
      </div>
      {resolved.error ? (
        <ErrorAlert error={resolved.error} title="权限配置有误" />
      ) : (
        resolved.data && <ToolsDisclosure resolved={resolved.data} />
      )}
    </div>
  )
}

/** 勾选的公共 MCP server（不归工具权限管）：agent 运行时连接它自己的私有 server 与这些 */
export function McpServersInput({ value, onChange }: { value: string[]; onChange: (v: string[]) => void }) {
  const servers = useLoad(() => api.mcp(null), [])
  return (
    <div className="flex flex-col gap-2">
      <div className="text-sm font-medium">公共 MCP</div>
      <div className="text-xs text-muted">勾选的公共 server 的工具给这个 agent；它自己的私有 server 总是可用</div>
      <Loading state={servers}>
        {(list) =>
          list.length === 0 ? (
            <span className="text-sm text-muted">没有公共 MCP server</span>
          ) : (
            <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
              {list.map((m) => (
                <label key={m.name} className="flex items-center gap-2 text-sm">
                  <ToggleSwitch
                    label={m.name}
                    isSelected={value.includes(m.name)}
                    onChange={(on) => onChange(on ? [...value, m.name] : value.filter((n) => n !== m.name))}
                  />
                  <span className="font-mono">{m.name}</span>
                  {!m.enabled && <span className="text-xs text-muted">已停用</span>}
                </label>
              ))}
            </div>
          )
        }
      </Loading>
    </div>
  )
}

function RoleForm({ role, onSaved }: { role?: Role; onSaved: () => void }) {
  const [name, setName] = useState(role?.name ?? '')
  const [description, setDescription] = useState(role?.description ?? '')
  const [allow, setAllow] = useState(role?.allow.join(' ') ?? '')
  const [deny, setDeny] = useState(role?.deny.join(' ') ?? '')
  const action = useAction()

  async function save() {
    const body: RoleInput = { description, allow: parsePatterns(allow), deny: parsePatterns(deny) }
    if (await action.run(() => (role ? api.updateRole(role.name, body) : api.createRole(name, body)))) onSaved()
  }

  return (
    <div className="flex flex-col gap-3">
      {!role && <TextInput label="名称" hint="小写字母、数字、- 和 _" mono value={name} onChange={setName} isRequired />}
      <TextInput label="描述" value={description} onChange={setDescription} isRequired />
      <TextInput label="允许" hint="通配符，空格分隔，如 issue:* web:*" mono value={allow} onChange={setAllow} />
      <TextInput label="禁止" hint="优先于所有允许（包括别的角色给的）" mono value={deny} onChange={setDeny} />
      <ErrorAlert error={action.error} />
      <ActionButton isPending={action.busy} onPress={() => void save()}>
        保存
      </ActionButton>
    </div>
  )
}

function RoleCard({ role, onChanged }: { role: Role; onChanged: () => void }) {
  const [editing, setEditing] = useState(false)
  const confirm = useConfirm()
  const action = useAction()

  async function remove() {
    const ok = await confirm({ title: `删除角色「${role.name}」？`, body: '有 agent 用着它时不能删。', confirmLabel: '删除', danger: true })
    if (ok && (await action.run(() => api.deleteRole(role.name)))) onChanged()
  }

  return (
    <Card>
      <Card.Header>
        <Card.Title className="flex flex-wrap items-center gap-2">
          <span className="font-mono">{role.name}</span>
          {role.builtin && (
            <Chip size="sm" variant="secondary">
              内置
            </Chip>
          )}
        </Card.Title>
        <Card.Description>{role.description}</Card.Description>
      </Card.Header>
      <Card.Content className="flex flex-col gap-2">
        <div className="font-mono text-xs text-muted">
          允许 {role.allow.join(' ') || '—'}
          {role.deny.length > 0 && <> · 禁止 {role.deny.join(' ')}</>}
        </div>
        <ToolsDisclosure resolved={role.resolved} title="能用的工具" />
        {editing && (
          <RoleForm
            role={role}
            onSaved={() => {
              setEditing(false)
              onChanged()
            }}
          />
        )}
        <ErrorAlert error={action.error} />
      </Card.Content>
      {!role.builtin && (
        <Card.Footer className="flex gap-2">
          <Button size="sm" variant="ghost" onPress={() => setEditing(!editing)}>
            <Pencil className="size-4" />
            {editing ? '取消' : '编辑'}
          </Button>
          <Button size="sm" variant="ghost" onPress={() => void remove()}>
            <Trash2 className="size-4" />
            删除
          </Button>
        </Card.Footer>
      )}
    </Card>
  )
}

/** 角色与权限：内置角色只读，自建角色可增删改；每个角色展开列出它能用的工具 */
export function Roles() {
  const roles = useLoad(() => api.roles(), [])
  const [formKey, setFormKey] = useState(0)
  return (
    <Page title="角色与权限">
      <p className="text-sm text-muted">
        每个工具有一个权限 id（资源:动作），角色是一组通配符 allow / deny，deny 优先。agent 的权限 = 它的角色 + 它自己的 allow / deny，在 agent 配置里选。MCP 不归权限管，在 agent 配置里勾选公共 server。
      </p>
      <CollapsibleForm title="新建角色">
        <RoleForm
          key={formKey}
          onSaved={() => {
            setFormKey((k) => k + 1)
            void roles.reload()
          }}
        />
      </CollapsibleForm>
      <Loading state={roles}>
        {(list) =>
          list.length === 0 ? (
            <EmptyHint>没有角色</EmptyHint>
          ) : (
            <div className="flex flex-col gap-2">
              {list.map((r) => (
                <RoleCard key={r.name} role={r} onChanged={() => void roles.reload()} />
              ))}
            </div>
          )
        }
      </Loading>
    </Page>
  )
}

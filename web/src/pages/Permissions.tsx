import { useState } from 'react'
import { useFieldDraft } from '../drafts'
import { useRefetch } from '../events'
import { Button, Card, Chip, CloseButton, ComboBox, Disclosure, Input, Label, ListBox, Description } from '@heroui/react'
import { Pencil, Trash2 } from 'lucide-react'
import { Link } from 'react-router-dom'
import { agentRoute, api, type AgentId, type Reach, type Role, type RoleInput, type ToolPermission } from '../api'
import { KIND_LABEL, ruleCandidates, splitRule } from '../rules'
import {
  ActionButton,
  CollapsibleForm,
  EmptyHint,
  ErrorAlert,
  Loading,
  Page,
  TextInput,
  ToggleSwitch,
  agentLabel,
  useAction,
  useAgents,
  useConfirm,
  useLoad,
} from '../ui'

const ACCESS: Record<ToolPermission['access'], { label: string; color: 'default' | 'warning' }> = {
  read: { label: '读', color: 'default' },
  write: { label: '写', color: 'warning' },
}

/** 工具标读 / 写与是否管理类 */
export function ToolLine({ tool }: { tool: ToolPermission }) {
  return (
    <div className="flex flex-wrap items-center gap-2">
      <Chip size="sm" variant="soft" color={ACCESS[tool.access].color}>
        {ACCESS[tool.access].label}
      </Chip>
      <span className="font-mono">{tool.name}</span>
      {tool.admin && <span className="text-xs text-muted">管理</span>}
    </div>
  )
}

/** 能用的资产（`kind:name`）：按种类成组，点进资产页 */
function AssetChips({ assets }: { assets: string[] }) {
  if (assets.length === 0) return <span className="text-sm text-muted">没有 skill、MCP、secret、记忆</span>
  return (
    <div className="flex flex-wrap gap-1.5">
      {assets.map((i) => {
        const { kind, name } = splitRule(i)
        return (
          <Link key={i} to={`/ai-assets?kind=${kind}`}>
            <Chip size="sm" variant="secondary">
              <span className="text-muted">{KIND_LABEL[kind]}</span> <span className="font-mono">{name}</span>
            </Chip>
          </Link>
        )
      })}
    </div>
  )
}

/** 一份配置能用的：资产直接列出，工具（较多）折叠 */
export function ReachView({ reach, what = '能用' }: { reach: Reach; what?: string }) {
  return (
    <div className="flex flex-col gap-2">
      <div className="text-sm text-muted">
        {what}的 skill、MCP、secret、记忆（{reach.assets.length}）
      </div>
      <AssetChips assets={reach.assets} />
      <Disclosure>
        <Disclosure.Heading>
          <Disclosure.Trigger className="flex items-center gap-1 text-sm text-muted">
            <Disclosure.Indicator className="ms-0" />
            {what}的工具（{reach.tools.length}）
          </Disclosure.Trigger>
        </Disclosure.Heading>
        <Disclosure.Content>
          <Disclosure.Body className="flex flex-col gap-1 pt-2 text-sm">
            {reach.tools.map((t) => (
              <ToolLine key={t.name} tool={t} />
            ))}
            {reach.tools.length === 0 && <span className="text-muted">没有工具</span>}
          </Disclosure.Body>
        </Disclosure.Content>
      </Disclosure>
    </div>
  )
}

/**
 * 一组 `action:kind:glob` 规则：已有的是可删的标签，输入框从现有资产（及每种的 `use:kind:*`）里补全，也可以直接写通配符，
 * 回车加入。拼错的（不匹配任何资产）由服务端在预览与保存时报出
 */
export function RulesInput({
  label,
  hint,
  value,
  onChange,
  candidates,
}: {
  label: string
  hint: string
  value: string[]
  onChange: (rules: string[]) => void
  candidates: string[]
}) {
  const [text, setText] = useState('')
  const add = (rule: string) => {
    const r = rule.trim()
    if (r && !value.includes(r)) onChange([...value, r])
    setText('')
  }
  const options = candidates.filter((c) => !value.includes(c))
  return (
    <div className="flex flex-col gap-1.5">
      <ComboBox
        fullWidth
        variant="secondary"
        allowsCustomValue
        inputValue={text}
        onInputChange={setText}
        selectedKey={null}
        onSelectionChange={(key) => {
          if (key !== null) add(String(key))
        }}
      >
        <Label>{label}</Label>
        <ComboBox.InputGroup>
          <Input
            className="font-mono"
            placeholder="输入 动作:种类:名字 或通配符，回车加入"
            onKeyDown={(e) => {
              if (e.key === 'Enter' && text.trim() && !options.includes(text.trim())) {
                e.preventDefault()
                add(text)
              }
            }}
          />
          <ComboBox.Trigger />
        </ComboBox.InputGroup>
        <Description>{hint}</Description>
        <ComboBox.Popover>
          <ListBox>
            {options.map((c) => (
              <ListBox.Item key={c} id={c} textValue={c}>
                <span className="font-mono text-sm">{c}</span>
              </ListBox.Item>
            ))}
          </ListBox>
        </ComboBox.Popover>
      </ComboBox>
      {value.length > 0 && (
        <div className="flex flex-wrap gap-1.5">
          {value.map((r) => (
            <Chip key={r} size="sm" variant="secondary" className="font-mono">
              {r}
              <CloseButton aria-label={`去掉 ${r}`} className="size-4" onPress={() => onChange(value.filter((x) => x !== r))} />
            </Chip>
          ))}
        </div>
      )}
    </div>
  )
}

/** 规则补全的候选：全部资产（含工具）及每种的 `use:kind:*` */
function useCandidates(): string[] {
  const assets = useLoad(() => api.assets(), [])
  useRefetch('agents', assets.reload)
  return ruleCandidates(assets.data ?? [])
}

export type PermissionValue = { roles: string[]; allow: string[]; deny: string[] }

/** agent 的角色开关、自己的 allow / deny，以及按它们算出的最终工具与资产（服务端计算；agentId 为 null 是还没建的） */
export function PermissionsInput({
  agentId,
  value,
  onChange,
}: {
  agentId: AgentId | null
  value: PermissionValue
  onChange: (v: PermissionValue) => void
}) {
  const roles = useLoad(() => api.roles(), [])
  useRefetch('agents', roles.reload)
  const candidates = useCandidates()
  const key = JSON.stringify(value)
  const reach = useLoad(() => api.previewAgent({ agent_id: agentId, ...value }), [agentId, key])
  return (
    <div className="flex flex-col gap-3">
      <div className="text-sm font-medium">角色与规则</div>
      <div className="text-xs text-muted">
        规则是 动作:种类:名字，动作 use（用）或 write（改）。能做的 = 它自己名下的（use 与 write）+ base（每个 agent 都有）+ 选中的角色 + 下面的 allow，deny 优先（不拦它自己名下的）。admin 是全部工具并能改任何资产。
      </div>
      <Loading state={roles}>
        {(list) => {
          const assignable = list.filter((r) => r.assignable)
          const names = assignable.map((r) => r.name)
          return (
            <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
              {assignable.map((r) => (
                // 不能用 <label> 包：开关自己是 label，嵌套时一次点击会切换两次
                <div key={r.name} className="flex items-center gap-2 text-sm">
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
                </div>
              ))}
            </div>
          )
        }}
      </Loading>
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        <RulesInput
          label="额外允许"
          hint="只给这个 agent 的，如 use:skill:relay-host、use:tool:issue_*、*:memory:trading"
          value={value.allow}
          onChange={(allow) => onChange({ ...value, allow })}
          candidates={candidates}
        />
        <RulesInput
          label="禁止"
          hint="优先于所有角色与允许，如 use:tool:trigger_*"
          value={value.deny}
          onChange={(deny) => onChange({ ...value, deny })}
          candidates={candidates}
        />
      </div>
      {reach.error ? (
        <ErrorAlert error={reach.error} title="权限配置有误" />
      ) : (
        reach.data && <ReachView reach={reach.data} what="最终能用" />
      )}
    </div>
  )
}

/** 角色的描述与规则（新建与编辑共用），值由调用方管；下面按规则预览保存后能用的 */
function RoleFields({
  name,
  value,
  set,
}: {
  name: string | null
  value: RoleInput
  set: (patch: Partial<RoleInput>) => void
}) {
  const candidates = useCandidates()
  const reach = useLoad(
    () => api.previewRole({ name, allow: value.allow, deny: value.deny }),
    [name, JSON.stringify([value.allow, value.deny])],
  )
  return (
    <>
      <TextInput label="描述" value={value.description} onChange={(description) => set({ description })} isRequired />
      <RulesInput
        label="收录（允许）"
        hint="动作:种类:名字，可用通配符，如 use:skill:relay-host、use:secret:RELAY_*、*:memory:trading（成员能用也能改）"
        value={value.allow}
        onChange={(allow) => set({ allow })}
        candidates={candidates}
      />
      <RulesInput
        label="禁止"
        hint="优先于所有允许（包括别的角色给的）"
        value={value.deny}
        onChange={(deny) => set({ deny })}
        candidates={candidates}
      />
      {reach.error ? <ErrorAlert error={reach.error} title="规则有误" /> : reach.data && <ReachView reach={reach.data} what="保存后能用" />}
    </>
  )
}

function RoleCreateForm({ onSaved }: { onSaved: () => void }) {
  const [name, setName] = useState('')
  const [value, setValue] = useState<RoleInput>({ description: '', allow: [], deny: [] })
  const action = useAction()
  return (
    <div className="flex flex-col gap-3">
      <TextInput label="名称" hint="小写字母、数字、- 和 _" mono value={name} onChange={setName} isRequired />
      <RoleFields name={null} value={value} set={(patch) => setValue((prev) => ({ ...prev, ...patch }))} />
      <ErrorAlert error={action.error} />
      <ActionButton
        isPending={action.busy}
        onPress={() => void action.run(() => api.createRole(name, value)).then((ok) => ok && onSaved())}
      >
        保存
      </ActionButton>
    </div>
  )
}

/** 编辑：只提交改过的字段，编辑期间 agent 改了 access.toml 里别的字段不会被旧值盖掉；没动过的字段跟着刷新 */
function RoleEditForm({ role, onSaved }: { role: Role; onSaved: () => void }) {
  const form = useFieldDraft<RoleInput>({ description: role.description, allow: role.allow, deny: role.deny })
  const action = useAction()
  return (
    <div className="flex flex-col gap-3">
      <RoleFields name={role.name} value={form.draft} set={form.set} />
      <ErrorAlert error={action.error} />
      <ActionButton
        isDisabled={Object.keys(form.changes).length === 0}
        isPending={action.busy}
        onPress={() => void action.run(() => api.updateRole(role.name, form.changes)).then((ok) => ok && onSaved())}
      >
        保存
      </ActionButton>
    </div>
  )
}

function RoleCard({ role, onChanged }: { role: Role; onChanged: () => void }) {
  const [editing, setEditing] = useState(false)
  const confirm = useConfirm()
  const action = useAction()
  const agents = useAgents()

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
      <Card.Content className="flex flex-col gap-3">
        <div className="flex flex-col gap-1 text-sm">
          {role.fixed_allow.length > 0 && <div className="text-muted">固定 {role.fixed_allow.length} 条工具规则（代码里定的，不能改）</div>}
          <div className="flex flex-wrap items-center gap-1.5">
            <span className="text-muted">收录</span>
            {role.allow.length === 0 && <span className="text-muted">—</span>}
            {role.allow.map((r) => (
              <Chip key={r} size="sm" variant="secondary" className="font-mono">
                {r}
              </Chip>
            ))}
          </div>
          {role.deny.length > 0 && (
            <div className="flex flex-wrap items-center gap-1.5">
              <span className="text-muted">禁止</span>
              {role.deny.map((r) => (
                <Chip key={r} size="sm" color="danger" variant="soft" className="font-mono">
                  {r}
                </Chip>
              ))}
            </div>
          )}
          <div className="flex flex-wrap items-center gap-1.5">
            <span className="text-muted">分给</span>
            {role.agents.length === 0 ? (
              <span className="text-muted">还没有 agent（在 agent 配置里勾选）</span>
            ) : (
              role.agents.map((id) => (
                <Link key={id} to={agentRoute(id)}>
                  <Chip size="sm" color="accent" variant="soft">
                    {agentLabel(agents.data, id)}
                  </Chip>
                </Link>
              ))
            )}
          </div>
        </div>
        <ReachView reach={role.reach} />
        {editing && (
          <RoleEditForm
            role={role}
            onSaved={() => {
              setEditing(false)
              onChanged()
            }}
          />
        )}
        <ErrorAlert error={action.error} />
      </Card.Content>
      <Card.Footer className="flex gap-2">
        <Button size="sm" variant="ghost" onPress={() => setEditing(!editing)}>
          <Pencil className="size-4" />
          {editing ? '取消' : '编辑'}
        </Button>
        {!role.builtin && (
          <Button size="sm" variant="ghost" onPress={() => void remove()}>
            <Trash2 className="size-4" />
            删除
          </Button>
        )}
      </Card.Footer>
    </Card>
  )
}

/** 角色：内置角色（base、admin）的固定规则只读、可编辑部分可改，不能删；自建角色可增删改。每个角色列出收录的资产与分给的 agent */
export function Roles() {
  const roles = useLoad(() => api.roles(), [])
  useRefetch('agents', roles.reload)
  const [formKey, setFormKey] = useState(0)
  return (
    <Page title="角色">
      <p className="text-sm text-muted">
        角色把 AI 资产（工具、skill、MCP、secret、记忆）的权限打包分给 agent：写 动作:种类:名字（动作 use 用、write 改，可用通配符），deny 优先。base 每个 agent 都有，收录进 base
        的人人可用；其余角色在 agent 配置里勾选，从它的下一次运行起生效。
      </p>
      <CollapsibleForm title="新建角色">
        <RoleCreateForm
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

import { useEffect, useState } from 'react'
import { Button, Card, Chip, Tabs, toast, type Key } from '@heroui/react'
import { Copy, MessageSquarePlus, Plus, Trash2 } from 'lucide-react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import {
  agentRoute,
  api,
  asAgentId,
  newSessionRoute,
  type Agent,
  type AgentDefaults,
  type AgentId,
  type AgentInput,
} from '../api'
import {
  EmptyHint,
  ErrorAlert,
  LinkButton,
  Loading,
  Markdown,
  Page,
  TextAreaInput,
  TextInput,
  formatTime,
  useAction,
  useAgents,
  useConfirm,
  useLoad,
} from '../ui'
import { SkillList } from './Skills'

export function AgentList() {
  const agents = useAgents()
  return (
    <Page
      title="Agents"
      actions={
        <LinkButton to="/agents/new">
          <Plus className="size-4" />
          新建
        </LinkButton>
      }
    >
      <Loading state={agents}>
        {(list) =>
          list.length === 0 ? (
            <EmptyHint>还没有 agent</EmptyHint>
          ) : (
            <div className="flex flex-col gap-2">
              {list.map((a) => (
                <Link key={a.id} to={agentRoute(a.id)}>
                  <Card className="transition-colors hover:bg-surface-hover">
                    <Card.Header>
                      <Card.Title>{a.name}</Card.Title>
                      <Card.Description>{a.description}</Card.Description>
                    </Card.Header>
                    <Card.Footer>
                      <Chip size="sm" variant="secondary">
                        {a.model}
                      </Chip>
                    </Card.Footer>
                  </Card>
                </Link>
              ))}
            </div>
          )
        }
      </Loading>
    </Page>
  )
}

function initialInput(agent: Agent | undefined, defaults: AgentDefaults | undefined): AgentInput {
  if (agent) {
    const { name, description, model, instructions, reflect_every } = agent
    return { name, description, model, instructions, reflect_every }
  }
  if (!defaults) throw new Error('agent defaults must be loaded before rendering the create form')
  return { name: '', description: '', model: defaults.model, instructions: '', reflect_every: defaults.reflect_every }
}

function AgentForm({ agent, defaults, onSaved }: { agent?: Agent; defaults?: AgentDefaults; onSaved: (a: Agent) => void }) {
  const [input, setInput] = useState<AgentInput>(() => initialInput(agent, defaults))
  const action = useAction()
  const set = (patch: Partial<AgentInput>) => setInput((prev) => ({ ...prev, ...patch }))

  async function save() {
    await action.run(async () => {
      const saved = agent ? await api.updateAgent(agent.id, input) : await api.createAgent(input)
      toast.success(agent ? '已保存' : '已创建')
      onSaved(saved)
    })
  }

  return (
    <div className="flex flex-col gap-4">
      <TextInput
        label="名称"
        hint="只用于显示，可随时修改，不能与其他 agent 重名"
        value={input.name}
        onChange={(name) => set({ name })}
        isRequired
      />
      <TextInput
        label="职责描述"
        hint="其他 agent 委派任务时据此选择"
        value={input.description}
        onChange={(description) => set({ description })}
        isRequired
      />
      <TextInput
        label="模型"
        hint="<provider 名>:<模型名>，provider 名来自服务端配置的 [llm.providers]，如 claude:claude-opus-5-5"
        value={input.model ?? ''}
        onChange={(model) => set({ model: model || null })}
        mono
      />
      <TextAreaInput
        label="指令（system prompt）"
        value={input.instructions}
        onChange={(instructions) => set({ instructions })}
        rows={8}
      />
      <TextInput
        label="反思间隔（轮）"
        hint="每完成这么多轮对话，强制回顾一次是否需要更新 skill 和记忆"
        type="number"
        value={input.reflect_every === null ? '' : String(input.reflect_every)}
        onChange={(v) => set({ reflect_every: v ? Number(v) : null })}
      />
      <ErrorAlert error={action.error} />
      <Button isPending={action.busy} onPress={() => void save()}>
        保存
      </Button>
    </div>
  )
}

export function AgentCreate() {
  const navigate = useNavigate()
  const defaults = useLoad(() => api.agentDefaults(), [])
  return (
    <Page title="新建 agent" back="/agents">
      <Card>
        <Card.Content>
          <Loading state={defaults}>
            {(d) => <AgentForm defaults={d} onSaved={(a) => navigate(agentRoute(a.id), { replace: true })} />}
          </Loading>
        </Card.Content>
      </Card>
    </Page>
  )
}

function MemoryEditor({ agentId }: { agentId: AgentId }) {
  const memory = useLoad(() => api.memory(agentId), [agentId])
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
            hint="agent 每次运行都会看到全部记忆，也会自行修改"
            value={content}
            onChange={setContent}
            rows={14}
            mono
          />
          <ErrorAlert error={action.error} />
          <Button
            isDisabled={content === m.content}
            isPending={action.busy}
            onPress={() =>
              void action.run(async () => {
                await api.setMemory(agentId, content)
                toast.success('记忆已保存')
                await memory.reload()
              })
            }
          >
            保存记忆
          </Button>
        </div>
      )}
    </Loading>
  )
}

function Reflections({ agentId }: { agentId: AgentId }) {
  const reflections = useLoad(() => api.reflections(agentId), [agentId])
  return (
    <Loading state={reflections}>
      {(list) =>
        list.length === 0 ? (
          <EmptyHint>还没有反思记录</EmptyHint>
        ) : (
          <div className="flex flex-col gap-2">
            {list.map((r) => (
              <Card key={r.id}>
                <Card.Header>
                  <Card.Description>
                    {formatTime(r.created_at)} · 消息 #{r.from_message_id + 1}–#{r.to_message_id}
                  </Card.Description>
                </Card.Header>
                <Card.Content>
                  <Markdown text={r.output} />
                </Card.Content>
              </Card>
            ))}
          </div>
        )
      }
    </Loading>
  )
}

async function copyText(text: string): Promise<void> {
  // 剪贴板 API 只在 HTTPS 等安全上下文可用；不可用时如实报错，文本本身可直接选中复制
  if (!navigator.clipboard) throw new Error('当前页面无法访问剪贴板（需要 HTTPS），请手动选中 id 复制')
  await navigator.clipboard.writeText(text)
  toast.success('已复制 id')
}

const DETAIL_TABS = [
  ['config', '配置'],
  ['memory', '记忆'],
  ['skills', 'Skills'],
  ['reflections', '反思'],
] as const

export function AgentDetail() {
  const id = asAgentId(useParams().id ?? '')
  const navigate = useNavigate()
  const confirm = useConfirm()
  const agent = useLoad(() => api.agent(id), [id])
  const [tab, setTab] = useState<Key>('config')
  const action = useAction()
  const name = agent.data?.name ?? id

  async function remove() {
    const ok = await confirm({
      title: `删除 agent「${name}」？`,
      body: '它的会话、记忆、私有 skill 和工作目录都会被删除，无法恢复。',
      confirmLabel: '删除',
      danger: true,
    })
    if (ok && (await action.run(() => api.deleteAgent(id)))) navigate('/agents')
  }

  return (
    <Page
      title={name}
      back="/agents"
      actions={
        <LinkButton to={newSessionRoute(id)} variant="secondary">
          <MessageSquarePlus className="size-4" />
          对话
        </LinkButton>
      }
    >
      <Loading state={agent}>
        {(a) => (
          <Tabs selectedKey={tab} onSelectionChange={setTab} variant="secondary">
            <Tabs.ListContainer>
              <Tabs.List aria-label="agent 详情">
                {DETAIL_TABS.map(([key, label]) => (
                  <Tabs.Tab key={key} id={key}>
                    {label}
                    <Tabs.Indicator />
                  </Tabs.Tab>
                ))}
              </Tabs.List>
            </Tabs.ListContainer>
            <Tabs.Panel id="config" className="flex flex-col gap-3 pt-3">
              <Card>
                <Card.Content className="flex flex-col gap-2 text-sm">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="text-muted">id</span>
                    <code className="selectable rounded bg-surface-secondary px-1.5 py-0.5 font-mono">{a.id}</code>
                    <Button size="sm" variant="ghost" onPress={() => void action.run(() => copyText(a.id))}>
                      <Copy className="size-4" />
                      复制
                    </Button>
                  </div>
                  <div className="text-muted">
                    距下次反思：{a.reflect_every - a.turns_since_reflection} 轮 · 创建于 {formatTime(a.created_at)}
                  </div>
                </Card.Content>
              </Card>
              <Card>
                <Card.Content>
                  <AgentForm key={`${a.id}-${a.name}`} agent={a} onSaved={() => void agent.reload()} />
                </Card.Content>
              </Card>
              <ErrorAlert error={action.error} />
              <Button variant="danger-soft" onPress={() => void remove()}>
                <Trash2 className="size-4" />
                删除 agent
              </Button>
            </Tabs.Panel>
            <Tabs.Panel id="memory" className="pt-3">
              <MemoryEditor agentId={a.id} />
            </Tabs.Panel>
            <Tabs.Panel id="skills" className="pt-3">
              <SkillList owner={a.id} />
            </Tabs.Panel>
            <Tabs.Panel id="reflections" className="pt-3">
              <Reflections agentId={a.id} />
            </Tabs.Panel>
          </Tabs>
        )}
      </Loading>
    </Page>
  )
}

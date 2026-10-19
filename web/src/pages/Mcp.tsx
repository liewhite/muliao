import { useState } from 'react'
import { Button, Card, Chip, toast } from '@heroui/react'
import { Plug, Trash2 } from 'lucide-react'
import { api, asAgentId, type Agent, type McpInput, type McpServer } from '../api'
import { CollapsibleForm, ToggleSwitch, ActionButton,
  EmptyHint,
  ErrorAlert,
  Loading,
  Page,
  Segmented,
  SelectInput,
  TextAreaInput,
  TextInput,
  agentLabel,
  useAction,
  useAgents,
  useConfirm,
  useLoad,
} from '../ui'

const PUBLIC = '@public'

function parsePairs(text: string): Record<string, string> {
  const result: Record<string, string> = {}
  for (const line of text.split('\n')) {
    if (!line.trim()) continue
    const idx = line.indexOf('=')
    if (idx <= 0) throw new Error(`无法解析「${line}」，格式应为 KEY=VALUE`)
    result[line.slice(0, idx).trim()] = line.slice(idx + 1).trim()
  }
  return result
}

function ToolChips({ tools }: { tools: string[] }) {
  return (
    <div className="flex flex-wrap gap-1.5">
      {tools.length === 0 ? (
        <span className="text-sm text-muted">已连接，没有工具</span>
      ) : (
        tools.map((t) => (
          <Chip key={t} size="sm" color="success" variant="soft">
            {t}
          </Chip>
        ))
      )}
    </div>
  )
}

function McpForm({ agents, onCreated }: { agents: Agent[] | undefined; onCreated: () => void }) {
  const [owner, setOwner] = useState(PUBLIC)
  const [name, setName] = useState('')
  const [transport, setTransport] = useState<'stdio' | 'http'>('stdio')
  const [command, setCommand] = useState('')
  const [args, setArgs] = useState('')
  const [url, setUrl] = useState('')
  const [pairs, setPairs] = useState('')
  const [tools, setTools] = useState<string[] | null>(null)
  const action = useAction()

  async function create() {
    const ok = await action.run(async () => {
      const kv = parsePairs(pairs)
      const input: McpInput = {
        owner: owner === PUBLIC ? null : asAgentId(owner),
        name,
        transport,
        command: transport === 'stdio' ? command : null,
        args:
          transport === 'stdio'
            ? args
                .split('\n')
                .map((a) => a.trim())
                .filter(Boolean)
            : [],
        url: transport === 'http' ? url : null,
        env: transport === 'stdio' ? kv : {},
        headers: transport === 'http' ? kv : {},
        probe: true,
      }
      const created = await api.addMcp(input)
      setTools(created.tools)
      toast.success(`已连接并保存 ${created.name}`)
    })
    if (ok) onCreated()
  }

  return (
    <CollapsibleForm title="添加 MCP server">
            <p className="text-sm text-muted">也可以直接在对话里把 MCP 信息告诉 agent，让它自己调用 mcp_add 创建。</p>
            <SelectInput
              label="归属"
              value={owner}
              onChange={setOwner}
              options={[
                { id: PUBLIC, label: '公共（所有 agent）' },
                ...(agents ?? []).map((a) => ({ id: a.id, label: `${a.name}（私有）` })),
              ]}
            />
            <TextInput label="名称" hint="工具名会以「名称_」为前缀" value={name} onChange={setName} mono isRequired />
            <div>
              <Segmented
                label="传输"
                showLabel
                value={transport}
                onChange={setTransport}
                options={[
                  { id: 'stdio', label: 'stdio（本地命令）' },
                  { id: 'http', label: 'HTTP / SSE' },
                ]}
              />
            </div>
            {transport === 'stdio' ? (
              <>
                <TextInput label="命令" value={command} onChange={setCommand} placeholder="npx" mono />
                <TextAreaInput label="参数（每行一个）" value={args} onChange={setArgs} rows={3} mono />
              </>
            ) : (
              <TextInput label="URL" value={url} onChange={setUrl} mono />
            )}
            <TextAreaInput
              label={transport === 'stdio' ? '环境变量（KEY=VALUE，每行一个）' : '请求头（Name=Value，每行一个）'}
              hint="secret 请先存入 Vault，再用 ${vault:KEY} 引用"
              value={pairs}
              onChange={setPairs}
              rows={3}
              mono
            />
            <ErrorAlert error={action.error} />
            {tools && <ToolChips tools={tools} />}
            <ActionButton isDisabled={!name} isPending={action.busy} onPress={() => void create()}>
              连接并保存
            </ActionButton>
          </CollapsibleForm>
  )
}

function McpItem({ server, agents, onChange }: { server: McpServer; agents: Agent[] | undefined; onChange: () => void }) {
  const confirm = useConfirm()
  const [tools, setTools] = useState<string[] | null>(null)
  const action = useAction()
  const target = server.command ? [server.command, ...server.args].join(' ') : server.url
  const pairs = { ...server.env, ...server.headers }

  async function remove() {
    const ok = await confirm({ title: `删除 MCP server「${server.name}」？`, confirmLabel: '删除', danger: true })
    if (ok) await action.run(() => api.deleteMcp(server.owner, server.name).then(onChange))
  }

  return (
    <Card>
      <Card.Header className="flex flex-row items-start gap-3">
        <div className="min-w-0 flex-1">
          <Card.Title className="font-mono text-base">{server.name}</Card.Title>
          <div className="mt-1 flex flex-wrap gap-1.5">
            <Chip size="sm" color="accent" variant="soft">
              {server.owner === null ? '公共' : agentLabel(agents, server.owner)}
            </Chip>
            <Chip size="sm" variant="secondary">
              {server.transport}
            </Chip>
          </div>
        </div>
        <ToggleSwitch
          label={`启用 MCP server「${server.name}」`}
          isSelected={server.enabled}
          isDisabled={action.busy}
          onChange={(enabled) => void action.run(() => api.setMcpEnabled(server.owner, server.name, enabled).then(onChange))}
        />
      </Card.Header>
      <Card.Content className="flex flex-col gap-2">
        <code className="block whitespace-pre-wrap break-all rounded-lg bg-surface-secondary p-2 font-mono text-xs">{target}</code>
        {Object.keys(pairs).length > 0 && (
          <code className="block whitespace-pre-wrap break-all rounded-lg bg-surface-secondary p-2 font-mono text-xs">
            {Object.entries(pairs)
              .map(([k, v]) => `${k}=${v}`)
              .join('\n')}
          </code>
        )}
        {tools && <ToolChips tools={tools} />}
        <ErrorAlert error={action.error} />
      </Card.Content>
      <Card.Footer className="flex gap-2">
        <ActionButton
          size="sm"
          variant="secondary"
          isPending={action.busy}
          onPress={() => void action.run(async () => setTools(await api.probeMcp(server.owner, server.name)))}
        >
          <Plug className="size-4" />
          测试连接
        </ActionButton>
        <Button size="sm" variant="danger-soft" isDisabled={action.busy} onPress={() => void remove()}>
          <Trash2 className="size-4" />
          删除
        </Button>
      </Card.Footer>
    </Card>
  )
}

export default function Mcp() {
  const servers = useLoad(() => api.mcp(), [])
  const agents = useAgents()
  return (
    <Page title="MCP" back="/settings">
      <McpForm agents={agents.data} onCreated={() => void servers.reload()} />
      <Loading state={servers}>
        {(list) =>
          list.length === 0 ? (
            <EmptyHint>没有 MCP server</EmptyHint>
          ) : (
            <div className="flex flex-col gap-3">
              {list.map((s) => (
                <McpItem key={`${s.id}-${s.enabled}`} server={s} agents={agents.data} onChange={() => void servers.reload()} />
              ))}
            </div>
          )
        }
      </Loading>
    </Page>
  )
}

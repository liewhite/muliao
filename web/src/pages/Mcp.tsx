import { useState } from 'react'
import { api, type McpInput, type McpServer } from '../api'
import { ErrorBanner, Field, Loading, Page, confirmAction, useAction, useLoad } from '../ui'

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

function McpForm({ onCreated }: { onCreated: () => void }) {
  const agents = useLoad(() => api.agents(), [])
  const [owner, setOwner] = useState('')
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
        owner: owner || null,
        name,
        transport,
        command: transport === 'stdio' ? command : null,
        args: transport === 'stdio' ? args.split('\n').map((a) => a.trim()).filter(Boolean) : [],
        url: transport === 'http' ? url : null,
        env: transport === 'stdio' ? kv : {},
        headers: transport === 'http' ? kv : {},
        probe: true,
      }
      const created = await api.addMcp(input)
      setTools(created.tools)
    })
    if (ok) onCreated()
  }

  return (
    <details className="card">
      <summary>添加 MCP server</summary>
      <div className="form">
        <p className="muted small">也可以直接在对话里把 MCP 信息告诉 agent，让它自己调用 mcp_add 创建。</p>
        <Field label="归属">
          <select value={owner} onChange={(e) => setOwner(e.target.value)}>
            <option value="">公共（所有 agent）</option>
            {agents.data?.map((a) => (
              <option key={a.name} value={a.name}>
                {a.name} 私有
              </option>
            ))}
          </select>
        </Field>
        <Field label="名称" hint="工具名会以「名称_」为前缀">
          <input value={name} onChange={(e) => setName(e.target.value)} autoCapitalize="off" />
        </Field>
        <Field label="传输">
          <select value={transport} onChange={(e) => setTransport(e.target.value as 'stdio' | 'http')}>
            <option value="stdio">stdio（本地命令）</option>
            <option value="http">http（Streamable HTTP / SSE）</option>
          </select>
        </Field>
        {transport === 'stdio' ? (
          <>
            <Field label="命令">
              <input value={command} onChange={(e) => setCommand(e.target.value)} className="mono" placeholder="npx" />
            </Field>
            <Field label="参数（每行一个）">
              <textarea rows={3} value={args} onChange={(e) => setArgs(e.target.value)} className="mono" />
            </Field>
          </>
        ) : (
          <Field label="URL">
            <input value={url} onChange={(e) => setUrl(e.target.value)} className="mono" />
          </Field>
        )}
        <Field
          label={transport === 'stdio' ? '环境变量（KEY=VALUE，每行一个）' : '请求头（Name=Value，每行一个）'}
          hint="secret 请先存入 Vault，再用 ${vault:KEY} 引用"
        >
          <textarea rows={3} value={pairs} onChange={(e) => setPairs(e.target.value)} className="mono" />
        </Field>
        <ErrorBanner error={action.error} />
        {tools && <div className="ok">已连接，工具：{tools.join(', ') || '（无）'}</div>}
        <button className="button primary" onClick={create} disabled={action.busy || !name}>
          {action.busy ? '连接中…' : '连接并保存'}
        </button>
      </div>
    </details>
  )
}

function McpItem({ server, onChange }: { server: McpServer; onChange: () => void }) {
  const [tools, setTools] = useState<string[] | null>(null)
  const action = useAction()
  const target = server.command ? [server.command, ...server.args].join(' ') : server.url
  const pairs = { ...server.env, ...server.headers }

  return (
    <li className="card">
      <div className="card-head">
        <strong>{server.name}</strong>
        <span className="tag">{server.owner ?? '公共'}</span>
        <span className="tag subtle">{server.transport}</span>
        {!server.enabled && <span className="tag subtle">已停用</span>}
      </div>
      <code className="block">{target}</code>
      {Object.keys(pairs).length > 0 && (
        <code className="block">
          {Object.entries(pairs)
            .map(([k, v]) => `${k}=${v}`)
            .join('\n')}
        </code>
      )}
      {tools && <div className="ok">工具：{tools.join(', ') || '（无）'}</div>}
      <ErrorBanner error={action.error} />
      <div className="row">
        <button className="button small" disabled={action.busy} onClick={() => void action.run(async () => setTools(await api.probeMcp(server.owner, server.name)))}>
          测试连接
        </button>
        <button
          className="button small"
          disabled={action.busy}
          onClick={() => void action.run(() => api.setMcpEnabled(server.owner, server.name, !server.enabled).then(onChange))}
        >
          {server.enabled ? '停用' : '启用'}
        </button>
        <button
          className="button small danger"
          disabled={action.busy}
          onClick={() => {
            if (confirmAction(`删除 MCP server ${server.name}？`)) void action.run(() => api.deleteMcp(server.owner, server.name).then(onChange))
          }}
        >
          删除
        </button>
      </div>
    </li>
  )
}

export default function Mcp() {
  const servers = useLoad(() => api.mcp(), [])
  return (
    <Page title="MCP" back="/settings">
      <McpForm onCreated={() => void servers.reload()} />
      <Loading state={servers}>
        {(list) => (
          <ul className="cards">
            {list.map((s) => (
              <McpItem key={`${s.id}-${s.enabled}`} server={s} onChange={() => void servers.reload()} />
            ))}
            {list.length === 0 && <div className="muted center">没有 MCP server</div>}
          </ul>
        )}
      </Loading>
    </Page>
  )
}

import { useState } from 'react'
import { Button, Card, Chip, Disclosure, Switch, toast } from '@heroui/react'
import { Pencil, Plus, Trash2 } from 'lucide-react'
import { Link } from 'react-router-dom'
import { api, asAgentId, type Agent, type CronJob } from '../api'
import {
  EmptyHint,
  ErrorAlert,
  Loading,
  Page,
  SelectInput,
  TextAreaInput,
  TextInput,
  agentLabel,
  formatTime,
  useAction,
  useAgents,
  useConfirm,
  useLoad,
} from '../ui'

const SCHEDULE_HINT = '5 段式 cron 表达式，按服务端配置文件中的 timezone 解释，如 */5 * * * *'

function CronItem({ job, agents, onChange }: { job: CronJob; agents: Agent[] | undefined; onChange: () => void }) {
  const confirm = useConfirm()
  const [editing, setEditing] = useState(false)
  const [schedule, setSchedule] = useState(job.schedule)
  const [prompt, setPrompt] = useState(job.prompt)
  const action = useAction()

  const update = (patch: { enabled?: boolean }) =>
    action.run(async () => {
      await api.updateCron(job.id, { schedule, prompt, enabled: patch.enabled ?? job.enabled })
      setEditing(false)
      onChange()
    })

  async function remove() {
    const ok = await confirm({ title: `删除定时任务「${job.name}」？`, confirmLabel: '删除', danger: true })
    if (ok) await action.run(() => api.deleteCron(job.id).then(onChange))
  }

  return (
    <Card>
      <Card.Header className="flex flex-row items-start gap-3">
        <div className="min-w-0 flex-1">
          <Card.Title>{job.name}</Card.Title>
          <div className="mt-1 flex flex-wrap items-center gap-1.5">
            <Chip size="sm" color="accent" variant="soft">
              {agentLabel(agents, job.agent_id)}
            </Chip>
            <code className="rounded bg-surface-secondary px-1.5 py-0.5 font-mono text-xs">{job.schedule}</code>
          </div>
        </div>
        <Switch
          size="sm"
          isSelected={job.enabled}
          isDisabled={action.busy}
          onChange={(enabled) => void update({ enabled })}
          aria-label={job.enabled ? '停用' : '启用'}
        >
          <Switch.Content>
            <Switch.Control>
              <Switch.Thumb />
            </Switch.Control>
          </Switch.Content>
        </Switch>
      </Card.Header>
      <Card.Content className="flex flex-col gap-3">
        <div className="text-xs text-muted">
          下次 {formatTime(job.next_run_at)}
          {job.last_run_at && <> · 上次 {formatTime(job.last_run_at)}</>}
          {job.last_session_id && (
            <>
              {' · '}
              <Link className="text-accent" to={`/sessions/${job.last_session_id}`}>
                上次会话
              </Link>
            </>
          )}
        </div>
        {job.last_error && <ErrorAlert title="触发失败，已自动停用" error={job.last_error} />}
        {editing ? (
          <>
            <TextInput label="cron 表达式" hint={SCHEDULE_HINT} value={schedule} onChange={setSchedule} mono />
            <TextAreaInput label="提示词" value={prompt} onChange={setPrompt} rows={5} />
          </>
        ) : (
          <p className="whitespace-pre-wrap text-sm">{job.prompt}</p>
        )}
        <ErrorAlert error={action.error} />
      </Card.Content>
      <Card.Footer className="flex gap-2">
        {editing ? (
          <>
            <Button size="sm" isPending={action.busy} onPress={() => void update({})}>
              保存
            </Button>
            <Button size="sm" variant="tertiary" onPress={() => setEditing(false)}>
              取消
            </Button>
          </>
        ) : (
          <>
            <Button size="sm" variant="secondary" onPress={() => setEditing(true)}>
              <Pencil className="size-4" />
              编辑
            </Button>
            <Button size="sm" variant="danger-soft" isDisabled={action.busy} onPress={() => void remove()}>
              <Trash2 className="size-4" />
              删除
            </Button>
          </>
        )}
      </Card.Footer>
    </Card>
  )
}

const EMPTY = { name: '', agent_id: '', schedule: '*/5 * * * *', prompt: '' }

function CronForm({ agents, onCreated }: { agents: Agent[] | undefined; onCreated: () => void }) {
  const [job, setJob] = useState(EMPTY)
  const action = useAction()
  const set = (patch: Partial<typeof EMPTY>) => setJob((prev) => ({ ...prev, ...patch }))

  async function create() {
    const ok = await action.run(async () => {
      await api.createCron({ ...job, agent_id: asAgentId(job.agent_id) })
      toast.success('定时任务已创建')
    })
    if (ok) {
      setJob(EMPTY)
      onCreated()
    }
  }

  return (
    <Card>
      <Disclosure>
        <Disclosure.Heading>
          <Disclosure.Trigger className="flex w-full items-center gap-2 px-1 font-medium">
            <Plus className="size-4" />
            新建定时任务
            <Disclosure.Indicator className="ml-auto" />
          </Disclosure.Trigger>
        </Disclosure.Heading>
        <Disclosure.Content>
          <Disclosure.Body className="flex flex-col gap-4 pt-4">
            <TextInput label="名称" value={job.name} onChange={(name) => set({ name })} isRequired />
            <SelectInput
              label="Agent"
              value={job.agent_id}
              onChange={(agent_id) => set({ agent_id })}
              placeholder="选择 agent"
              options={(agents ?? []).map((a) => ({ id: a.id, label: a.name }))}
            />
            <TextInput label="cron 表达式" hint={SCHEDULE_HINT} value={job.schedule} onChange={(schedule) => set({ schedule })} mono />
            <TextAreaInput
              label="提示词"
              hint="每次触发都会在新会话中执行。定时会话不会自动推送，需要时在提示词里要求 agent 调用 notify_user"
              value={job.prompt}
              onChange={(prompt) => set({ prompt })}
              rows={5}
            />
            <ErrorAlert error={action.error} />
            <Button
              isDisabled={!job.name || !job.agent_id || !job.prompt}
              isPending={action.busy}
              onPress={() => void create()}
            >
              创建
            </Button>
          </Disclosure.Body>
        </Disclosure.Content>
      </Disclosure>
    </Card>
  )
}

export default function Cron() {
  const jobs = useLoad(() => api.cron(), [])
  const agents = useAgents()
  return (
    <Page title="定时任务">
      <CronForm agents={agents.data} onCreated={() => void jobs.reload()} />
      <Loading state={jobs}>
        {(list) =>
          list.length === 0 ? (
            <EmptyHint>没有定时任务</EmptyHint>
          ) : (
            <div className="flex flex-col gap-3">
              {list.map((j) => (
                <CronItem
                  key={`${j.id}-${j.schedule}-${j.enabled}-${j.prompt}`}
                  job={j}
                  agents={agents.data}
                  onChange={() => void jobs.reload()}
                />
              ))}
            </div>
          )
        }
      </Loading>
    </Page>
  )
}

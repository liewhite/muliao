import { useEffect, useState } from 'react'
import { Button, Card, Chip, toast } from '@heroui/react'
import { Plus, Trash2 } from 'lucide-react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import { agentRoute, api, asAgentId, type AgentId, type Skill } from '../api'
import { CorruptList } from './CorruptList'
import { ActionButton,
  EmptyHint,
  ErrorAlert,
  LinkButton,
  Loading,
  Page,
  TextAreaInput,
  TextInput,
  useAction,
  useConfirm,
  useLoad,
} from '../ui'

function editorPath(owner: AgentId | null, name?: string): string {
  const params = new URLSearchParams()
  if (owner) params.set('owner', owner)
  if (name) params.set('name', name)
  return `/skills/edit?${params}`
}

/** owner 为 null 列出公共 skill，否则列出该 agent 的私有 skill。 */
export function SkillList({ owner }: { owner: AgentId | null }) {
  const skills = useLoad(() => api.skills(owner), [owner])
  return (
    <div className="flex flex-col gap-3">
      <div>
        <LinkButton to={editorPath(owner)} variant="secondary">
          <Plus className="size-4" />
          新建 skill
        </LinkButton>
      </div>
      <Loading state={skills}>
        {(list) =>
          list.length === 0 ? (
            <EmptyHint>没有 skill</EmptyHint>
          ) : (
            <div className="flex flex-col gap-2">
              {list.map((s) => (
                <Link key={s.name} to={editorPath(owner, s.name)}>
                  <Card className="transition-colors hover:bg-surface-hover">
                    <Card.Header>
                      <Card.Title className="font-mono text-base">{s.name}</Card.Title>
                      <Card.Description>{s.description}</Card.Description>
                    </Card.Header>
                  </Card>
                </Link>
              ))}
            </div>
          )
        }
      </Loading>
      <CorruptList kind="skill" owner={owner} />
    </div>
  )
}

export function PublicSkills() {
  return (
    <Page title="公共 Skills">
      <p className="text-sm text-muted">所有 agent 都能看到公共 skill；同名私有 skill 会遮蔽它。</p>
      <SkillList owner={null} />
    </Page>
  )
}

export function SkillEditor() {
  const [params] = useSearchParams()
  const navigate = useNavigate()
  const confirm = useConfirm()
  const ownerParam = params.get('owner')
  const owner = ownerParam === null ? null : asAgentId(ownerParam)
  const existing = params.get('name')
  const back = owner ? agentRoute(owner) : '/skills'
  const [skill, setSkill] = useState<Skill>({ owner, name: existing ?? '', description: '', body: '' })
  const loaded = useLoad(() => (existing ? api.skill(owner, existing) : Promise.resolve(null)), [owner, existing])
  const action = useAction()
  useEffect(() => {
    if (loaded.data) setSkill(loaded.data)
  }, [loaded.data])

  const set = (patch: Partial<Skill>) => setSkill((prev) => ({ ...prev, ...patch }))

  async function save() {
    const ok = await action.run(async () => {
      await api.saveSkill(skill)
      toast.success('skill 已保存')
    })
    if (ok) navigate(back)
  }

  async function remove() {
    if (!existing) return
    const ok = await confirm({ title: `删除 skill「${existing}」？`, confirmLabel: '删除', danger: true })
    if (ok && (await action.run(() => api.deleteSkill(owner, existing)))) navigate(back)
  }

  return (
    <Page title={existing ?? '新建 skill'} back={back}>
      <Loading state={loaded}>
        {() => (
          <Card>
            <Card.Content className="flex flex-col gap-4">
              <div>
                <Chip size="sm" variant="secondary">
                  {owner ? '私有 skill' : '公共 skill'}
                </Chip>
              </div>
              {!existing && (
                <TextInput
                  label="名称"
                  hint="小写字母、数字、- 或 _"
                  value={skill.name}
                  onChange={(name) => set({ name })}
                  mono
                  isRequired
                />
              )}
              <TextInput
                label="描述"
                hint="何时使用这个 skill，会出现在 agent 的 skill 索引里"
                value={skill.description}
                onChange={(description) => set({ description })}
                isRequired
              />
              <TextAreaInput label="内容（Markdown）" value={skill.body} onChange={(body) => set({ body })} rows={18} mono />
              <ErrorAlert error={action.error} />
              <div className="flex gap-2">
                <ActionButton isPending={action.busy} onPress={() => void save()}>
                  保存
                </ActionButton>
                {existing && (
                  <Button variant="danger-soft" isDisabled={action.busy} onPress={() => void remove()}>
                    <Trash2 className="size-4" />
                    删除
                  </Button>
                )}
              </div>
            </Card.Content>
          </Card>
        )}
      </Loading>
    </Page>
  )
}

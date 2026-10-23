import { useEffect, useRef, useState } from 'react'
import { Button, Card, Chip, toast } from '@heroui/react'
import { Download, Plus, Trash2, Upload } from 'lucide-react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import { agentRoute, api, ApiError, asAgentId, type AgentId, type Schemas, type Skill } from '../api'
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
      <div className="flex flex-wrap gap-2">
        <LinkButton to={editorPath(owner)} variant="secondary">
          <Plus className="size-4" />
          新建 skill
        </LinkButton>
        <ImportSkill owner={owner} onImported={() => void skills.reload()} />
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

/** 导入标准 .skill 文件为 owner 的 skill；同名已存在时问是否覆盖，没有导入的文件列在下面。 */
function ImportSkill({ owner, onImported }: { owner: AgentId | null; onImported: () => void }) {
  const input = useRef<HTMLInputElement>(null)
  const confirm = useConfirm()
  const action = useAction()
  const [result, setResult] = useState<Schemas['SkillImportOut'] | null>(null)

  async function importFile(file: File) {
    setResult(null)
    await action.run(async () => {
      let imported: Schemas['SkillImportOut']
      try {
        imported = await api.importSkill(owner, file, false)
      } catch (e) {
        if (!(e instanceof ApiError && e.status === 409)) throw e
        const ok = await confirm({
          title: '同名 skill 已存在，覆盖它？',
          body: '覆盖后它的 SKILL.md 与附件换成包里的版本，旧版本留在历史里。',
          confirmLabel: '覆盖',
          danger: true,
        })
        if (!ok) return
        imported = await api.importSkill(owner, file, true)
      }
      setResult(imported)
      toast.success(`已导入 skill「${imported.skill.name}」`)
      onImported()
    })
  }

  return (
    <>
      <input
        ref={input}
        type="file"
        accept=".skill,.zip"
        hidden
        onChange={(e) => {
          const file = e.target.files?.[0]
          // 允许再次选择同一个文件
          e.target.value = ''
          if (file) void importFile(file)
        }}
      />
      <ActionButton variant="secondary" size="sm" isPending={action.busy} onPress={() => input.current?.click()}>
        <Upload className="size-4" />
        导入 .skill
      </ActionButton>
      {action.error && (
        <div className="w-full">
          <ErrorAlert error={action.error} />
        </div>
      )}
      {result && result.skipped.length > 0 && (
        <div className="w-full text-sm text-muted">
          <div>以下文件没有导入：</div>
          <ul className="list-disc pl-5">
            {result.skipped.map((s) => (
              <li key={s.path}>
                <span className="font-mono">{s.path}</span>：{s.reason}
              </li>
            ))}
          </ul>
        </div>
      )}
    </>
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

  async function exportPackage() {
    if (!existing) return
    await action.run(async () => {
      const url = URL.createObjectURL(await api.exportSkill(owner, existing))
      const link = document.createElement('a')
      link.href = url
      link.download = `${existing}.skill`
      link.click()
      // 点击触发的下载异步开始，太早释放会取消它
      setTimeout(() => URL.revokeObjectURL(url), 10_000)
    })
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
                  <Button variant="secondary" isDisabled={action.busy} onPress={() => void exportPackage()}>
                    <Download className="size-4" />
                    导出 .skill
                  </Button>
                )}
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

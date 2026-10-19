import { useEffect, useState } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import { agentRoute, api, asAgentId, type AgentId, type Skill } from '../api'
import { ErrorBanner, Field, Loading, Page, confirmAction, useAction, useLoad } from '../ui'

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
    <>
      <Link className="button small" to={editorPath(owner)}>
        新建 skill
      </Link>
      <Loading state={skills}>
        {(list) => (
          <ul className="list">
            {list.map((s) => (
              <li key={s.name}>
                <Link className="list-item" to={editorPath(owner, s.name)}>
                  <div className="list-main">
                    <div className="list-title">{s.name}</div>
                    <div className="list-sub">{s.description}</div>
                  </div>
                </Link>
              </li>
            ))}
            {list.length === 0 && <div className="muted center">没有 skill</div>}
          </ul>
        )}
      </Loading>
    </>
  )
}

export function PublicSkills() {
  return (
    <Page title="公共 Skills" back="/settings">
      <p className="muted small">所有 agent 都能看到公共 skill；同名私有 skill 会遮蔽它。</p>
      <SkillList owner={null} />
    </Page>
  )
}

export function SkillEditor() {
  const [params] = useSearchParams()
  const navigate = useNavigate()
  const ownerParam = params.get('owner')
  const owner = ownerParam === null ? null : asAgentId(ownerParam)
  const existing = params.get('name')
  const back = owner ? agentRoute(owner) : '/skills'
  const [skill, setSkill] = useState<Skill>({ owner, name: existing ?? '', description: '', body: '' })
  const loaded = useLoad(
    () => (existing ? api.skill(owner, existing) : Promise.resolve(null)),
    [owner, existing],
  )
  const action = useAction()
  useEffect(() => {
    if (loaded.data) setSkill(loaded.data)
  }, [loaded.data])

  const set = (patch: Partial<Skill>) => setSkill((prev) => ({ ...prev, ...patch }))

  async function save() {
    if (await action.run(() => api.saveSkill(skill))) navigate(back)
  }

  async function remove() {
    if (!existing || !confirmAction(`删除 skill ${existing}？`)) return
    if (await action.run(() => api.deleteSkill(owner, existing))) navigate(back)
  }

  return (
    <Page title={existing ?? '新建 skill'} back={back}>
      <Loading state={loaded}>
        {() => (
          <div className="form">
            <div className="muted small">{owner ? `${owner} 的私有 skill` : '公共 skill'}</div>
            {!existing && (
              <Field label="名称">
                <input value={skill.name} onChange={(e) => set({ name: e.target.value })} autoCapitalize="off" />
              </Field>
            )}
            <Field label="描述" hint="何时使用这个 skill，会出现在 agent 的 skill 索引里">
              <input value={skill.description} onChange={(e) => set({ description: e.target.value })} />
            </Field>
            <Field label="内容（Markdown）">
              <textarea rows={18} className="mono" value={skill.body} onChange={(e) => set({ body: e.target.value })} />
            </Field>
            <ErrorBanner error={action.error} />
            <div className="row">
              <button className="button primary" onClick={save} disabled={action.busy}>
                保存
              </button>
              {existing && (
                <button className="button danger" onClick={remove} disabled={action.busy}>
                  删除
                </button>
              )}
            </div>
          </div>
        )}
      </Loading>
    </Page>
  )
}

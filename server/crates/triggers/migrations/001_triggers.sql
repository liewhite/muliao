-- 触发器：具名、持久的事件来源（docs/architecture.md 第 14 节）。列与 Python 版相同，旧库原样迁移。
-- 怎么触发由 kind 决定，只在代码里多态实现，表里不为某种方式开专门的列：config 是这种方式的配置（用户或 agent 设置），
-- state 是这种方式自己维护的运行状态（如下次触发时间），都是 JSON。
CREATE TABLE triggers (
    -- id 是会话的发送方身份（sessions.origin_ref），必须永不复用
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    -- 小写字母、数字与 -。只在未结束的触发器之间唯一（见 finished_at）
    name TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    -- 交给 agent 的说明；事件内容（若有）附在其后的 <event> 里
    prompt TEXT NOT NULL,
    kind TEXT NOT NULL,
    config TEXT NOT NULL,
    state TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    last_fired_at TEXT,
    -- 上一次触发开出的会话（可能后来被删，不设外键）：防重叠时问会话领域它忙不忙
    last_session_id TEXT,
    -- 触发前的前置检查（JSON），所有方式共用；NULL 表示每次都交给 agent
    precheck TEXT,
    -- 最近一次跑前置检查的时间（不论结果）
    last_checked_at TEXT,
    -- fire_when = changed 时最近一次成功检查的输出（比较基线）；换前置检查时清空
    precheck_output TEXT,
    -- 被自动停用的原因（如存下的数据坏了）；重新启用时清除
    disabled_reason TEXT,
    -- 脚本出错（JSON 列表：前置检查失败、流式命令反复退出），同一来源只留一条
    faults TEXT NOT NULL DEFAULT '[]',
    -- 不会再触发了（一次性触发器用掉了它那一次）。行保留，它开过的会话仍查得到来源
    finished_at TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX ix_triggers_agent_id ON triggers (agent_id);
-- 名称只在未结束的触发器之间唯一：结束之后同名可以再建
CREATE UNIQUE INDEX uq_triggers_name_unfinished ON triggers (name) WHERE finished_at IS NULL;

-- issue 领域的表（docs/architecture.md 第 21 节）。时间都是 UTC 的文本形式。agent 与会话只按 id 引用，没有跨领域外键。

-- 人与 agent 协作的一件事
CREATE TABLE issues (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    -- backlog / in_progress / in_review / blocked / done / cancelled
    status TEXT NOT NULL,
    -- none / urgent / high / medium / low
    priority TEXT NOT NULL,
    -- 负责的项目（agent id）：创建时必填，不随指派变；只在负责的 agent 被删除后为空
    owner_agent_id TEXT,
    -- 现在谁在做：agent id、user，或空（未指派）
    assignee TEXT,
    -- 创建者：agent id，用户创建的为 user
    creator TEXT NOT NULL,
    created_at TEXT NOT NULL,
    -- 时间线上最新一条的时间
    updated_at TEXT NOT NULL
);
CREATE INDEX ix_issues_status ON issues (status);
CREATE INDEX ix_issues_assignee ON issues (assignee);
CREATE INDEX ix_issues_owner ON issues (owner_agent_id);
CREATE INDEX ix_issues_updated ON issues (updated_at);

-- 时间线：评论与每一次属性变化，只增不改。kind、body、change 的形状由 activity.rs 的 Happening 定义
CREATE TABLE issue_activities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    issue_id INTEGER NOT NULL REFERENCES issues (id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    -- 做这件事的人：agent id、user，或 system（删除 agent 时的清理）
    actor TEXT NOT NULL,
    kind TEXT NOT NULL,
    body TEXT,
    -- JSON：{"from": 原值, "to": 新值}（上游完成另带 issue）
    change TEXT,
    -- 发生在哪个会话（agent 用工具时所在的）；用户在界面上的操作、系统的清理为空；会话删除时置空（订阅 SessionDeleted）
    session_id TEXT
);
CREATE INDEX ix_issue_activities_issue ON issue_activities (issue_id, id);
CREATE INDEX ix_issue_activities_session ON issue_activities (session_id);
CREATE INDEX ix_issue_activities_actor ON issue_activities (actor, created_at);

-- 依赖（有向无环）：issue_id 依赖 depends_on_id（上游）。任一端被删除，这条边随之删除
CREATE TABLE issue_dependencies (
    issue_id INTEGER NOT NULL REFERENCES issues (id) ON DELETE CASCADE,
    depends_on_id INTEGER NOT NULL REFERENCES issues (id) ON DELETE CASCADE,
    PRIMARY KEY (issue_id, depends_on_id)
);
CREATE INDEX ix_issue_dependencies_upstream ON issue_dependencies (depends_on_id);

-- 会话领域的表（docs/architecture.md 第 7 节）。时间都是 UTC 的文本形式。

CREATE TABLE sessions (
    id TEXT PRIMARY KEY NOT NULL,
    agent_id TEXT NOT NULL,
    title TEXT NOT NULL,
    -- 来源：user / trigger / delegation / issue（origins.rs 一处解析）
    origin TEXT NOT NULL,
    -- 具体的发送方编号：触发器 id、issue id；用户、委派开的为空
    origin_ref TEXT,
    -- 委派开出的 thread：父会话与 thread 名（父会话删除后置空，thread 保留）
    parent_session_id TEXT REFERENCES sessions (id) ON DELETE SET NULL,
    thread TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX ix_sessions_agent ON sessions (agent_id);
CREATE INDEX ix_sessions_updated ON sessions (updated_at);
CREATE INDEX ix_sessions_origin_ref ON sessions (origin, origin_ref);
CREATE INDEX ix_sessions_parent ON sessions (parent_session_id);
CREATE UNIQUE INDEX uq_sessions_thread ON sessions (parent_session_id, agent_id, thread)
    WHERE parent_session_id IS NOT NULL;

-- 运行记录：会话 actor 的一次运行（或一次手动压缩），开始时写下，结束时记结局。信件交给了哪次运行、消息由哪次运行
-- 保存都指向它；没结束的就是停在半路（重启后接着跑）
CREATE TABLE runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions (id) ON DELETE CASCADE,
    -- run / compaction
    kind TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    -- done / stopped / failed；没结束时为空
    outcome TEXT,
    -- 最终失败的说明
    error TEXT
);
CREATE INDEX ix_runs_session ON runs (session_id, id);
CREATE INDEX ix_runs_unfinished ON runs (session_id) WHERE ended_at IS NULL;

-- 历史：每行一条 agenter Message 的 JSON（存储形态）。id 单调递增（AUTOINCREMENT，删尾部行后不复用）
CREATE TABLE messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions (id) ON DELETE CASCADE,
    run_id INTEGER NOT NULL REFERENCES runs (id) ON DELETE CASCADE,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX ix_messages_session ON messages (session_id, id);
CREATE INDEX ix_messages_created ON messages (created_at);
CREATE INDEX ix_messages_run ON messages (run_id);

-- 消息里可按关键字搜到的文字（写消息时同一事务写入；没有文字的消息没有这一行）
CREATE TABLE message_texts (
    message_id INTEGER PRIMARY KEY NOT NULL REFERENCES messages (id) ON DELETE CASCADE,
    text TEXT NOT NULL
);

-- 压缩：id <= through_message_id 的消息由 summary 代替发给模型；at_message_id 是写入时会话最后一条消息
CREATE TABLE compactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions (id) ON DELETE CASCADE,
    through_message_id INTEGER NOT NULL,
    at_message_id INTEGER NOT NULL,
    summary TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX ix_compactions_session ON compactions (session_id, id);

-- 哪次工具调用开出或续写了哪个 thread；要交回结局的（ask_agent、report_back）就是一项委派，这一行是它的唯一主体
CREATE TABLE thread_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    parent_session_id TEXT NOT NULL REFERENCES sessions (id) ON DELETE CASCADE,
    call_id TEXT NOT NULL,
    thread_session_id TEXT NOT NULL REFERENCES sessions (id) ON DELETE CASCADE,
    -- report_back 的心跳间隔（秒）；其余为空
    check_seconds REAL
);
CREATE UNIQUE INDEX uq_thread_calls_call ON thread_calls (parent_session_id, call_id);
CREATE INDEX ix_thread_calls_thread ON thread_calls (thread_session_id);

-- 信箱
CREATE TABLE mail (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions (id) ON DELETE CASCADE,
    -- message / answer / event
    kind TEXT NOT NULL,
    -- 事件的来源（job:12、thread:<会话>）；其余为空
    source TEXT,
    -- 经来信入口投进来的发送方来源（消息必有；用户的回答为 user；事件与交回的结局为空）
    origin TEXT,
    content TEXT NOT NULL,
    -- 运行中检查（事件的一种）
    check_in INTEGER NOT NULL DEFAULT 0,
    -- 附件的文件 id（JSON 数组）
    attachments TEXT NOT NULL DEFAULT '[]',
    -- 回答：调用 id -> 结果（JSON 对象）
    answers TEXT NOT NULL DEFAULT '{}',
    -- 用户在等它的回答（处理它的运行结束时推送）
    awaited INTEGER NOT NULL,
    -- 委派来的信：哪项委派（发起会话删除时置空）
    delegation_id INTEGER REFERENCES thread_calls (id) ON DELETE SET NULL,
    -- queued / consumed / discarded
    state TEXT NOT NULL,
    -- 交给了哪次运行
    taken_by INTEGER REFERENCES runs (id) ON DELETE CASCADE,
    created_at TEXT NOT NULL
);
CREATE INDEX ix_mail_queued ON mail (session_id) WHERE state = 'queued';
CREATE INDEX ix_mail_session ON mail (session_id, id);
CREATE INDEX ix_mail_created ON mail (created_at);
CREATE INDEX ix_mail_delegation ON mail (delegation_id) WHERE delegation_id IS NOT NULL;
CREATE INDEX ix_mail_taken_by ON mail (taken_by) WHERE taken_by IS NOT NULL;

-- 占用：别的工作替会话在外面做的一项异步工作。行永久保留：带 call_id 的是「这次挂起调用已交出去」的永久事实
CREATE TABLE session_holds (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions (id) ON DELETE CASCADE,
    -- 外部工作的占用者（job:12、wake:<会话>:<调用>）；委派的为空，引用委派行
    holder TEXT,
    thread_call_id INTEGER REFERENCES thread_calls (id) ON DELETE SET NULL,
    call_id TEXT,
    awaited INTEGER NOT NULL,
    -- active / released / cleared
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    ended_at TEXT
);
CREATE INDEX ix_holds_session ON session_holds (session_id);
CREATE INDEX ix_holds_active ON session_holds (session_id) WHERE status = 'active';
CREATE UNIQUE INDEX uq_holds_holder ON session_holds (holder) WHERE holder IS NOT NULL;
CREATE UNIQUE INDEX uq_holds_thread_call ON session_holds (thread_call_id) WHERE thread_call_id IS NOT NULL;

-- 到点唤醒（wake_me）：还在不在等看同 holder 的占用是否 active
CREATE TABLE wakeups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    holder TEXT NOT NULL UNIQUE,
    session_id TEXT NOT NULL REFERENCES sessions (id) ON DELETE CASCADE,
    due_at TEXT NOT NULL,
    note TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX ix_wakeups_session ON wakeups (session_id);

-- 停下来等用户批准的调用（运行收尾时记下）：区分「等批准」与「执行到一半被打断」
CREATE TABLE approval_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions (id) ON DELETE CASCADE,
    call_id TEXT NOT NULL,
    -- 工具请求批准时定下的被批准对象的指纹，批准时交回工具比对；迁移来的旧行可能为空
    pin TEXT,
    created_at TEXT NOT NULL,
    -- 批准第一次交给运行去执行的时间：之后再交就是重复执行，改给「被中断」
    taken_at TEXT
);
CREATE UNIQUE INDEX uq_approval_requests_call ON approval_requests (session_id, call_id);

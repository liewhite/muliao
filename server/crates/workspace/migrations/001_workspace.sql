-- 工作区领域的表。时间都是 UTC 的文本形式。

-- 后台任务：agent 在后台跑的一条 shell 命令，不占住运行，结束时、运行中每隔一段时间往发起它的会话的信箱投事件（jobs.rs）。
-- 只进入终态、不删除；不引用会话表（会话删除后留着，job_status 照样查得到）
CREATE TABLE jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    command TEXT NOT NULL,
    -- 注入的 vault key（JSON 数组，不存值）
    secrets TEXT NOT NULL,
    -- running / exited / killed / interrupted
    status TEXT NOT NULL,
    exit_code INTEGER,
    -- 输出日志，相对 agent 工作目录（agent 用 read_file 读）
    log_path TEXT NOT NULL,
    -- 进程（组）号；进程还没起来时为空
    pid INTEGER,
    -- 运行中检查的间隔（秒，run_shell 的 heartbeat_seconds）
    check_seconds REAL NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT
);
CREATE INDEX ix_jobs_status ON jobs (status);
CREATE INDEX ix_jobs_agent ON jobs (agent_id);

-- 用户对一个 skill 脚本调用的「始终允许」（#531，grants.rs）：之后同一个 agent 的同一个调用（同一 skill、脚本、参数、
-- secret，且 skill 目录里除 SKILL.md 外的内容没变）不再停下等批准。存库不放资源目录：agent 能写自己的资源目录，等于自己
-- 批准自己。用户在设置页查看、撤销（删行）；agent 删除时一起删
CREATE TABLE script_grants (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id TEXT NOT NULL,
    skill TEXT NOT NULL,
    -- 脚本相对 skill 目录的路径（解析后的）
    script TEXT NOT NULL,
    -- JSON 数组
    args TEXT NOT NULL,
    -- 注入的 vault key（排序去重的 JSON 数组，不存值）
    secrets TEXT NOT NULL,
    -- skill 目录除 SKILL.md 外的内容指纹
    fingerprint TEXT NOT NULL,
    -- 以上各项（含 agent_id）合起来的摘要：查找与去重按它
    key TEXT NOT NULL UNIQUE,
    -- 用户点「始终允许」的那次调用：所在会话与调用 id（只作来历，会话删了也留着）
    session_id TEXT NOT NULL,
    call_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    -- 按它直接放行的次数与最后一次的时间
    hits INTEGER NOT NULL DEFAULT 0,
    last_used_at TEXT
);
CREATE INDEX ix_script_grants_agent ON script_grants (agent_id);

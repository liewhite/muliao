-- 一笔模型用量：会话运行每完成一次模型请求一行，压缩与运行收尾时把还没记上的部分一行。只增不改，不随 agent 或会话的
-- 删除而消失（账目：删掉资源不等于钱没花过），所以 agent_id、session_id 只是不透明的 id。
-- input_tokens 是输入总数，已经包含 cache_read_tokens 与 cache_write_tokens。
CREATE TABLE model_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    session_id TEXT,
    kind TEXT NOT NULL,
    model TEXT NOT NULL,
    requests INTEGER NOT NULL,
    input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    cache_read_tokens INTEGER NOT NULL,
    cache_write_tokens INTEGER NOT NULL
) STRICT;
-- 汇总按时间范围取行；会话页取一个会话的累计
CREATE INDEX ix_model_usage_created_at ON model_usage (created_at);
CREATE INDEX ix_model_usage_session_id ON model_usage (session_id);

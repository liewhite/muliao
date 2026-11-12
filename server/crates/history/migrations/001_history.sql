-- 工作历史（docs/architecture.md 第 22 节）：agent 做过的事，一件一行，只追加。agent 只按 id 引用（和用量一样是不透明的 id），
-- 不随 agent 删除而消失。时间是 UTC 的文本形式，day 是配置时区的日历日（YYYY-MM-DD）。
CREATE TABLE agent_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id TEXT NOT NULL,
    -- 工作发生那天（不是写入的日子）
    day TEXT NOT NULL,
    title TEXT NOT NULL,
    -- 2–5 行：做了什么、结果、重点
    summary TEXT NOT NULL,
    -- 引用（#12、会话 id、commit 等）：字符串的 JSON 数组
    refs TEXT NOT NULL,
    -- 1 一般，2 重要，3 很重要
    importance INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
-- 按 agent 与日期取（某天写过没有、按时间倒序列出）
CREATE INDEX ix_agent_history_agent_day ON agent_history (agent_id, day);

-- 全文检索（trigram 分词，中文可用），rowid 即 agent_history.id。行不会改或删，所以只需要插入时同步
CREATE VIRTUAL TABLE agent_history_fts USING fts5(title, summary, refs, tokenize='trigram');
CREATE TRIGGER agent_history_fts_insert AFTER INSERT ON agent_history BEGIN
    INSERT INTO agent_history_fts (rowid, title, summary, refs)
    VALUES (new.id, new.title, new.summary, (SELECT group_concat(value, ' ') FROM json_each(new.refs)));
END;

-- 只增不改：改和删在库里直接失败
CREATE TRIGGER agent_history_no_update BEFORE UPDATE ON agent_history BEGIN
    SELECT RAISE(ABORT, 'agent_history is append-only');
END;
CREATE TRIGGER agent_history_no_delete BEFORE DELETE ON agent_history BEGIN
    SELECT RAISE(ABORT, 'agent_history is append-only');
END;

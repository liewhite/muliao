-- 委派方式只存一份（#847）：以前没有存下来，靠占用、挂起记录和 check_seconds 拼出来。thread_calls 加 mode 列，
-- 只有 report_back 带间隔（CHECK 约束）。旧行换算：有间隔的是 report_back；发起方在这次调用上挂起成 ask_agent 的是 ask；
-- 其余是普通 delegate_task（plain；线上有 1 行 #823 之前的 ask_agent 没有挂起记录、记成 plain，无占用与信件引用它，
-- 只是历史记录标错方式，不为它按工具名推断）。重建表以加上跨列约束；迁移连接已关外键，子表（mail、session_holds）按表名引用，
-- 改名后仍指向新表；自增序号搬过去，删过的委派号不被重用。
CREATE TABLE thread_calls_new (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    parent_session_id TEXT NOT NULL REFERENCES sessions (id) ON DELETE CASCADE,
    call_id TEXT NOT NULL,
    thread_session_id TEXT NOT NULL REFERENCES sessions (id) ON DELETE CASCADE,
    -- plain（普通 delegate_task）/ report_back / ask（ask_agent）
    mode TEXT NOT NULL CHECK (mode IN ('plain', 'report_back', 'ask')),
    -- report_back 的心跳间隔（秒）；其余为空
    check_seconds REAL CHECK ((mode = 'report_back') = (check_seconds IS NOT NULL))
);

INSERT INTO thread_calls_new (id, parent_session_id, call_id, thread_session_id, mode, check_seconds)
SELECT t.id, t.parent_session_id, t.call_id, t.thread_session_id,
    CASE
        WHEN t.check_seconds IS NOT NULL THEN 'report_back'
        WHEN EXISTS (
            SELECT 1 FROM paused_calls p
            WHERE p.session_id = t.parent_session_id AND p.call_id = t.call_id AND p.kind = 'ask_agent'
        ) THEN 'ask'
        ELSE 'plain'
    END,
    t.check_seconds
FROM thread_calls t ORDER BY t.id;

DELETE FROM sqlite_sequence WHERE name = 'thread_calls_new';
INSERT INTO sqlite_sequence (name, seq) SELECT 'thread_calls_new', seq FROM sqlite_sequence WHERE name = 'thread_calls';

DROP TABLE thread_calls;
ALTER TABLE thread_calls_new RENAME TO thread_calls;
CREATE UNIQUE INDEX uq_thread_calls_call ON thread_calls (parent_session_id, call_id);
CREATE INDEX ix_thread_calls_thread ON thread_calls (thread_session_id);

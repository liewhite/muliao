-- 挂起的调用统一记一张表（#823）：以前问用户按工具名认、问 agent 按带调用号的委派占用认、等批准按 approval_requests
-- 认；现在每个挂起的调用在挂起时记一行，写明种类。approval_requests 并进来（等批准独有的 pin、taken_at 两列只在等批准
-- 时有值），删掉旧表。换算现有数据：
-- - 等批准：原样搬过来，旧行没有 pin 的记空串（工具给的指纹不会是空的，批准后比对对不上、不执行，与以前读法一致）；
-- - 问 agent：带调用号的委派占用（不论状态）就是交出去了的 ask_agent；换算后删掉占用上的调用号列；
-- - 问用户：参数里有问题、还没有结果的 ask_user 调用（没结果的调用只可能在会话最后一条回复里）。
CREATE TABLE paused_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions (id) ON DELETE CASCADE,
    call_id TEXT NOT NULL,
    -- ask_user / ask_agent / approval
    kind TEXT NOT NULL CHECK (kind IN ('ask_user', 'ask_agent', 'approval')),
    -- 等批准：工具请求批准时定下的被批准对象的指纹，批准时交回工具比对
    pin TEXT CHECK ((kind = 'approval') = (pin IS NOT NULL)),
    -- 等批准：批准第一次交给运行去执行的时间，之后再交就是重复执行，改给「被中断」
    taken_at TEXT CHECK (kind = 'approval' OR taken_at IS NULL),
    created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX uq_paused_calls_call ON paused_calls (session_id, call_id);

INSERT INTO paused_calls (session_id, call_id, kind, pin, taken_at, created_at)
SELECT session_id, call_id, 'approval', COALESCE(pin, ''), taken_at, created_at FROM approval_requests ORDER BY id;

INSERT OR IGNORE INTO paused_calls (session_id, call_id, kind, created_at)
SELECT session_id, call_id, 'ask_agent', created_at FROM session_holds WHERE call_id IS NOT NULL ORDER BY id;
-- 占用上的调用号是第三份（委派行 thread_calls.call_id 已有，挂起与否看这张表），删掉
ALTER TABLE session_holds DROP COLUMN call_id;

INSERT OR IGNORE INTO paused_calls (session_id, call_id, kind, created_at)
SELECT m.session_id, json_extract(p.value, '$.id'), 'ask_user', m.created_at
FROM messages m, json_each(m.payload, '$.parts') p
WHERE m.payload LIKE '%"ask_user"%'
    AND json_extract(m.payload, '$.role') = 'assistant'
    AND json_extract(p.value, '$.type') = 'tool_call'
    AND json_extract(p.value, '$.name') = 'ask_user'
    AND json_valid(json_extract(p.value, '$.args'))
    AND json_array_length(json_extract(p.value, '$.args'), '$.questions') > 0
    AND NOT EXISTS (
        SELECT 1 FROM messages r, json_each(r.payload, '$.parts') q
        WHERE r.session_id = m.session_id
            AND r.id > m.id
            AND r.payload LIKE '%' || json_extract(p.value, '$.id') || '%'
            AND json_extract(q.value, '$.type') = 'tool_result'
            AND json_extract(q.value, '$.call_id') = json_extract(p.value, '$.id'))
ORDER BY m.id;

DROP TABLE approval_requests;

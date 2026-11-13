-- 占用不再用字符串当键（#805）：后台任务的占用存它的任务编号（job_id），到点唤醒的行按 hold_id 引用它的占用；
-- 占用者 holder（`job:<id>`、`wake:<会话>:<调用>`）删掉。唤醒的会话从占用行得出，wakeups.session_id 一并删掉。
ALTER TABLE session_holds ADD COLUMN job_id INTEGER;
UPDATE session_holds SET job_id = CAST(substr(holder, 5) AS INTEGER) WHERE kind = 'job';
CREATE UNIQUE INDEX uq_holds_job ON session_holds (job_id) WHERE job_id IS NOT NULL;

CREATE TABLE wakeups_new (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    hold_id INTEGER NOT NULL UNIQUE REFERENCES session_holds (id) ON DELETE CASCADE,
    due_at TEXT NOT NULL,
    note TEXT NOT NULL,
    created_at TEXT NOT NULL
);
INSERT INTO wakeups_new (id, hold_id, due_at, note, created_at)
SELECT w.id, h.id, w.due_at, w.note, w.created_at FROM wakeups w JOIN session_holds h ON h.holder = w.holder;
DROP TABLE wakeups;
ALTER TABLE wakeups_new RENAME TO wakeups;

DROP INDEX uq_holds_holder;
ALTER TABLE session_holds DROP COLUMN holder;

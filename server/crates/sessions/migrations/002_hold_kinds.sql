-- 占用按种类区分（job / wake / delegation）：以前靠 holder 前缀与 thread_call_id 是否为空来认，委派的 thread 被删除后
-- thread_call_id 置空，种类就丢了。按 holder 前缀回填；holder 为空的都是委派（含 thread 已删除、行引用已置空的）。
ALTER TABLE session_holds ADD COLUMN kind TEXT NOT NULL DEFAULT '';
UPDATE session_holds SET kind = CASE
    WHEN holder LIKE 'job:%' THEN 'job'
    WHEN holder LIKE 'wake:%' THEN 'wake'
    WHEN holder IS NULL THEN 'delegation'
    ELSE kind
END;

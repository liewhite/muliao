-- 时间线里指派人变化（kind = 'assignee'）的 change 从字符串（`user` 或 agent id）改成按种类打标签的对象（#822）：
-- {"kind":"user"} / {"kind":"agent","agent_id":"…"}；未指派仍是 null。
UPDATE issue_activities
SET change = json_object(
    'from', json(CASE
        WHEN json_extract(change, '$.from') IS NULL THEN NULL
        WHEN json_extract(change, '$.from') = 'user' THEN '{"kind":"user"}'
        ELSE json_object('kind', 'agent', 'agent_id', json_extract(change, '$.from'))
    END),
    'to', json(CASE
        WHEN json_extract(change, '$.to') IS NULL THEN NULL
        WHEN json_extract(change, '$.to') = 'user' THEN '{"kind":"user"}'
        ELSE json_object('kind', 'agent', 'agent_id', json_extract(change, '$.to'))
    END)
)
WHERE kind = 'assignee';

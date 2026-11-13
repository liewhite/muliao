-- 请求里每段信件内容是什么改记在 metadata 的 muliao_parts（每段一条）：事件 {part, event, check_in}，消息
-- {part, sender}。以前事件是两张下标表（muliao_events 全部事件、muliao_check_ins 其中的检查），来源在读时从正文
-- `<event source="X">\n…` 反解析；哪句是用户说的在读时拿信件原文去匹配。这里各换算一次。

-- 1. 事件：来源按旧的正文格式取出。
UPDATE messages SET payload = json_remove(json_set(payload, '$.metadata.muliao_parts', (
    SELECT json_group_array(json_object(
        'part', e.value,
        'event', (SELECT substr(t, 16, instr(t, '">') - 16)
                  FROM (SELECT json_extract(messages.payload, '$.parts[' || e.value || '].text') AS t)),
        'check_in', json(CASE WHEN EXISTS (
            SELECT 1 FROM json_each(messages.payload, '$.metadata.muliao_check_ins') c WHERE c.value = e.value
        ) THEN 'true' ELSE 'false' END)))
    FROM json_each(messages.payload, '$.metadata.muliao_events') e
)), '$.metadata.muliao_events', '$.metadata.muliao_check_ins')
WHERE json_type(payload, '$.metadata.muliao_events') = 'array';

-- 2. 消息：同一次运行交出的消息信件，正文与请求里某段文字完全相同的，那段与紧跟其后的附件段（信件有几个附件就几段）
-- 记下信件的发送方（只有附件、没有文字的信件认不出，按会话的发起方算）。
CREATE TEMP TABLE texts AS
SELECT m.id AS message_id, CAST(p.key AS INTEGER) AS part, min(mail.origin) AS origin,
       max(json_array_length(coalesce(mail.attachments, '[]'))) AS files
FROM messages m
JOIN json_each(m.payload, '$.parts') p
JOIN mail ON mail.session_id = m.session_id AND mail.taken_by = m.run_id
    AND mail.kind = 'message' AND mail.state = 'consumed'
    AND json_extract(p.value, '$.type') = 'text' AND json_extract(p.value, '$.text') = mail.content
WHERE json_extract(m.payload, '$.role') = 'user' AND trim(mail.content) <> ''
GROUP BY m.id, p.key;

CREATE TEMP TABLE senders AS
SELECT message_id, part, origin FROM texts
UNION
SELECT t.message_id, CAST(q.key AS INTEGER), t.origin
FROM texts t
JOIN messages m ON m.id = t.message_id
JOIN json_each(m.payload, '$.parts') q
    ON CAST(q.key AS INTEGER) > t.part AND CAST(q.key AS INTEGER) <= t.part + t.files
    AND json_extract(q.value, '$.type') = 'file';

UPDATE messages SET payload = json_set(payload, '$.metadata',
    json_set(CASE WHEN json_type(payload, '$.metadata') = 'object' THEN json_extract(payload, '$.metadata')
                  ELSE '{}' END,
        '$.muliao_parts', (
        SELECT json_group_array(json(mark)) FROM (
            SELECT part, mark FROM (
                SELECT CAST(json_extract(x.value, '$.part') AS INTEGER) AS part, x.value AS mark
                FROM json_each(messages.payload, '$.metadata.muliao_parts') x
                UNION ALL
                SELECT s.part, json_object('part', s.part, 'sender', s.origin)
                FROM senders s WHERE s.message_id = messages.id)
            ORDER BY part))))
WHERE id IN (SELECT message_id FROM senders);

DROP TABLE senders;
DROP TABLE texts;

-- 工具挂给用户的文件只存文件编号、名字与类型（{file_id, name, media_type}），地址与能否内联读时派生；以前存的是派生好的
-- {url, name, media_type, inline}。文件编号从地址 `/api/files/<编号>/<名字>` 取出。两处：工具结果的 metadata
-- （muliao_attachments），ask_agent 交回的结局（回答信件里 AgentAnswer 的 attachments）。

UPDATE messages SET payload = json_set(payload, '$.parts', json((
    SELECT json_group_array(json(CASE
        WHEN json_type(p.value, '$.metadata.muliao_attachments') = 'array' THEN json_set(
            p.value, '$.metadata.muliao_attachments', json((
                SELECT json_group_array(json_object(
                    'file_id', substr(u, 12, instr(substr(u, 12), '/') - 1),
                    'name', json_extract(a.value, '$.name'),
                    'media_type', json_extract(a.value, '$.media_type')) ORDER BY CAST(a.key AS INTEGER))
                FROM (SELECT key, value, json_extract(value, '$.url') AS u
                      FROM json_each(p.value, '$.metadata.muliao_attachments')) a)))
        ELSE p.value
    END) ORDER BY CAST(p.key AS INTEGER))
    FROM json_each(messages.payload, '$.parts') p)))
WHERE payload LIKE '%muliao_attachments%';

UPDATE mail SET answers = (
    SELECT json_group_object(x.key, json(CASE
        WHEN json_extract(x.value, '$.tool') = 'ask_agent'
             AND json_type(x.value, '$.result.attachments') = 'array' THEN json_set(
            x.value, '$.result.attachments', json((
                SELECT json_group_array(json_object(
                    'file_id', substr(u, 12, instr(substr(u, 12), '/') - 1),
                    'name', json_extract(a.value, '$.name'),
                    'media_type', json_extract(a.value, '$.media_type')) ORDER BY CAST(a.key AS INTEGER))
                FROM (SELECT key, value, json_extract(value, '$.url') AS u
                      FROM json_each(x.value, '$.result.attachments')) a)))
        ELSE x.value
    END))
    FROM json_each(mail.answers) x)
WHERE kind = 'answer' AND answers LIKE '%"attachments"%';

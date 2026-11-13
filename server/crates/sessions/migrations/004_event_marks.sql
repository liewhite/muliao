-- 请求里的事件段改成在 metadata 里各记一条 {part, source, check_in}（muliao_events），不再是两张下标表
-- （muliao_events 全部事件、muliao_check_ins 其中的检查）加上读时从正文反解析来源。来源按旧的正文格式
-- `<event source="X">\n…` 取一次。
UPDATE messages SET payload = json_remove(json_set(payload, '$.metadata.muliao_events', (
    SELECT json_group_array(json_object(
        'part', e.value,
        'source', (SELECT substr(t, 16, instr(t, '">') - 16)
                   FROM (SELECT json_extract(messages.payload, '$.parts[' || e.value || '].text') AS t)),
        'check_in', json(CASE WHEN EXISTS (
            SELECT 1 FROM json_each(messages.payload, '$.metadata.muliao_check_ins') c WHERE c.value = e.value
        ) THEN 'true' ELSE 'false' END)))
    FROM json_each(messages.payload, '$.metadata.muliao_events') e
)), '$.metadata.muliao_check_ins')
WHERE json_type(payload, '$.metadata.muliao_events[0]') = 'integer';

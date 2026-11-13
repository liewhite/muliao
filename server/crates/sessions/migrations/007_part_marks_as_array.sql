-- 004 上线时把子查询的结果直接交给 json_set，丢了 JSON 子类型：muliao_parts 存成了一个字符串（内容是数组），既有
-- 事件又有发送方的请求，事件那几条还被整个套成了外层数组里的一个元素。这里解析回数组、展平、按段排序。数据正确时不改任何行。
UPDATE messages SET payload = json_set(payload, '$.metadata.muliao_parts', json((
    SELECT json_group_array(json(mark) ORDER BY json_extract(mark, '$.part')) FROM (
        SELECT e.value AS mark
        FROM json_each(json_extract(messages.payload, '$.metadata.muliao_parts')) e WHERE e.type = 'object'
        UNION ALL
        SELECT f.value
        FROM json_each(json_extract(messages.payload, '$.metadata.muliao_parts')) e, json_each(e.value) f
        WHERE e.type = 'array'))))
WHERE json_type(payload, '$.metadata.muliao_parts') = 'text';

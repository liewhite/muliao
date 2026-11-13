-- 用户对等批准的调用的决定改存一个选择（approve / deny / always），不再是 approved、always 两个布尔（能写出「拒绝且始终
-- 允许」）。回答信件里每个 approval 结果按旧的两个布尔换算，缺的按 false。
UPDATE mail SET answers = (
    SELECT json_group_object(key, CASE
        WHEN json_extract(value, '$.tool') = 'approval' THEN json_object(
            'tool', 'approval',
            'result', json_object(
                'choice', CASE
                    WHEN NOT coalesce(json_extract(value, '$.result.approved'), 0) THEN 'deny'
                    WHEN coalesce(json_extract(value, '$.result.always'), 0) THEN 'always'
                    ELSE 'approve'
                END,
                'pin', json_extract(value, '$.result.pin')))
        ELSE json(value)
    END)
    FROM json_each(mail.answers))
WHERE kind = 'answer' AND answers LIKE '%"approval"%';

-- 运行记录只记会话的运行：手动压缩不再写运行记录（没有地方读它），删掉已有的这类行与 kind 列。
DELETE FROM runs WHERE kind = 'compaction';
ALTER TABLE runs DROP COLUMN kind;

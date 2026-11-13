-- 始终允许的查找键改成直接序列化（#783），不再仿 Python 的 json.dumps：已有的键都是旧写法、再也对不上，删掉，
-- 用户下次批准时重新点「始终允许」（上线时只有 1 条）
DELETE FROM script_grants;

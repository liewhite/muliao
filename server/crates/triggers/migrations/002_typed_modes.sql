-- #798：一行的 kind/config/state 读出时按种类一次解析，读不懂就是这一行读不出来，不再有「存下的数据坏了自动停用」及其原因。
ALTER TABLE triggers DROP COLUMN disabled_reason;
-- 挡路的上一次会话在等待时，已为哪个会话通知过用户（定时与流式共用，每个挡路会话只通知一次）。定时原来记在 state 的
-- skipped.notified 里，搬过来。
ALTER TABLE triggers ADD COLUMN waiting_notified TEXT;
UPDATE triggers SET waiting_notified = json_extract(state, '$.skipped.session_id')
WHERE kind = 'schedule' AND json_extract(state, '$.skipped.notified') = 1;
-- 流式进程的运行状况（启动时间、连续失败次数）改放内存，state 只留 generation。Python 时代建的可能没有 generation，
-- 补一个（下次托管按它起）。
UPDATE triggers
SET state = json_object('generation', coalesce(json_extract(state, '$.generation'), lower(hex(randomblob(16)))))
WHERE kind = 'stream';

-- 通知不再认识 issue：点开去哪由发送方给一个页面地址（link），收件箱只存它、按它标已读。以前存的是 issue 编号，点开的
-- 地址由收件箱拼成 issue 页 `/issues/<编号>`；这里换算成同一个地址。
ALTER TABLE notifications ADD COLUMN link TEXT;
UPDATE notifications SET link = '/issues/' || issue_id WHERE issue_id IS NOT NULL;
DROP INDEX ix_notifications_issue;
ALTER TABLE notifications DROP COLUMN issue_id;
-- 打开页面时按它标已读
CREATE INDEX ix_notifications_link ON notifications (link);

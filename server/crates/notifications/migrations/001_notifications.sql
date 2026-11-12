-- 通知领域的表（docs/architecture.md 第 14 节）。时间都是 UTC 的文本形式。

-- 发给用户的一条通知。通知页与应用角标的未读数是它的展示，Web Push 是它的投递，内容不另存。
-- id 兼作「已读到哪一条」的游标，必须单调且与提交顺序一致：AUTOINCREMENT 不复用，单写者让分配顺序即提交顺序
CREATE TABLE notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    -- 通知关于的会话（没有 issue_id 时点开打开它），随会话删除（订阅 SessionDeleted，不设跨领域外键）；系统通知为空
    session_id TEXT,
    -- 交给用户审核的 issue：点开打开它，打开它即已读。issue 被删后照旧指向它（点开看到「不存在」）
    issue_id INTEGER,
    -- 打开通知页、它关于的会话或 issue 即为已读
    unread INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);
CREATE INDEX ix_notifications_session ON notifications (session_id);
CREATE INDEX ix_notifications_issue ON notifications (issue_id);
-- 未读数、标已读只看未读的那几行
CREATE INDEX ix_notifications_unread ON notifications (id) WHERE unread;

-- 浏览器推送订阅（一台设备一行）。endpoint 的完整路径即订阅凭据，不进日志
CREATE TABLE push_subscriptions (
    endpoint TEXT PRIMARY KEY NOT NULL,
    p256dh TEXT NOT NULL,
    auth TEXT NOT NULL,
    created_at TEXT NOT NULL
);

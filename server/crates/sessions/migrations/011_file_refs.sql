-- 消息里的文件引用不再存接口路径：`/api/files/<编号>/<名字>` 改成 `muliao-file:<编号>/<名字>`（files::FileRef），界面
-- 用的地址读时派生，改接口路由不再让旧消息里的附件认不出。引用只在文件段的 "url" 键上（用户消息的附件、工具结果里
-- 存下的图片）；JSON 字符串内容里的引号都转义成 \"，所以 `"url":"/api/files/` 只会匹配到真正的键，回答正文里的
-- markdown 链接（文字）不动。
UPDATE messages SET payload = replace(payload, '"url":"/api/files/', '"url":"muliao-file:')
WHERE payload LIKE '%"url":"/api/files/%';

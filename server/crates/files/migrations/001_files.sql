-- 对话里出现的一个文件的元数据；内容按 sha256 存在文件目录里（见 muliao-files）。写入后不再改变，也不删除。
CREATE TABLE files (
    id TEXT PRIMARY KEY NOT NULL,
    sha256 TEXT NOT NULL,
    name TEXT NOT NULL,
    media_type TEXT NOT NULL,
    size INTEGER NOT NULL,
    created_at TEXT NOT NULL
) STRICT;

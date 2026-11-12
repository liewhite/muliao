//! 记录的 id：随机 UUID v4 的字符串形式（与 Python 版相同，旧数据的 id 原样可用）。

pub fn new_id() -> String {
    uuid::Uuid::new_v4().to_string()
}

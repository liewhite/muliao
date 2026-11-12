//! 用户与 agent 写的时刻（ISO 8601）怎么读：带时区的按它的时区，不带的按服务端配置的时区（用户与 agent 说的都是本地
//! 时间）。到点唤醒与一次性触发器共用这一条规则。

use chrono::{DateTime, NaiveDateTime, TimeZone, Utc};
use chrono_tz::Tz;

/// 解析一个 ISO 8601 时刻，如 `2026-10-22T10:30`、`2026-10-22 10:30:00`、`2026-10-22T10:30+08:00`、`...Z`。读不懂（或
/// 这个本地时刻在时区里不存在）时 None，由调用方报它自己的错。
pub fn parse_local(text: &str, tz: Tz) -> Option<DateTime<Utc>> {
    // `Z` 表示 UTC：换成 +00:00，带不带秒都按带时区的解析
    let normalized = text.trim().strip_suffix('Z').map(|t| format!("{t}+00:00"));
    let text = normalized.as_deref().unwrap_or(text.trim());
    let offset = DateTime::parse_from_rfc3339(text)
        .ok()
        .or_else(|| DateTime::parse_from_str(text, "%Y-%m-%dT%H:%M%:z").ok())
        .or_else(|| DateTime::parse_from_str(text, "%Y-%m-%dT%H:%M%z").ok());
    if let Some(d) = offset {
        return Some(d.with_timezone(&Utc));
    }
    let naive = ["%Y-%m-%dT%H:%M:%S%.f", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"]
        .iter()
        .find_map(|f| NaiveDateTime::parse_from_str(text, f).ok())?;
    Some(tz.from_local_datetime(&naive).earliest()?.with_timezone(&Utc))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn local_unless_an_offset_is_given() {
        let tz: Tz = "Asia/Shanghai".parse().unwrap();
        let utc = |s: &str| DateTime::parse_from_rfc3339(s).unwrap().with_timezone(&Utc);
        assert_eq!(parse_local("2026-10-22T10:30", tz), Some(utc("2026-10-22T02:30:00Z")));
        assert_eq!(parse_local(" 2026-10-22 10:30:15 ", tz), Some(utc("2026-10-22T02:30:15Z")));
        assert_eq!(parse_local("2026-10-22T10:30Z", tz), Some(utc("2026-10-22T10:30:00Z")));
        assert_eq!(parse_local("2026-10-22T10:30+09:00", tz), Some(utc("2026-10-22T01:30:00Z")));
        assert_eq!(parse_local("2026-10-22T10:30:00.5+00:00", tz), Some(utc("2026-10-22T10:30:00.5Z")));
        assert_eq!(parse_local("tomorrow", tz), None);
        assert_eq!(parse_local("1767225600", tz), None);
    }
}

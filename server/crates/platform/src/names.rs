//! 枚举的名字（存库、写进文件、报错与给人看）：只认它的 serde 名字（`#[serde(rename_all = …)]`），名字只写那一份，
//! 不另写 `as_str` / `parse` 的对照表。
//!
//! 只有不带数据的变体（或 untagged 的这类枚举）有名字。枚举在定义处写一次 [`serde_names!`](crate::serde_names)，得到
//! `Display`、`FromStr` 与存库用的 `ToSql` / `FromSql`。

use serde::Serialize;
use serde::de::{DeserializeOwned, IntoDeserializer};

#[doc(hidden)]
pub use rusqlite as __rusqlite;

/// value 的 serde 名字。不是单个名字（带数据的变体）是调用方的 bug：当场 panic。
pub fn name<T: Serialize + ?Sized>(value: &T) -> String {
    match serde_json::to_value(value) {
        Ok(serde_json::Value::String(s)) => s,
        other => panic!("{} has no serde name: {other:?}", std::any::type_name::<T>()),
    }
}

/// 按 serde 名字读回；不认识时的报错是 serde 的（列出全部合法的名字）。
pub fn parse<T: DeserializeOwned>(name: &str) -> Result<T, String> {
    T::deserialize(name.into_deserializer()).map_err(|e: serde::de::value::Error| e.to_string())
}

/// 给只有名字的枚举实现 `Display`、`FromStr`（Err 为说明）与 rusqlite 的 `ToSql` / `FromSql`，都按 serde 名字。
#[macro_export]
macro_rules! serde_names {
    ($($t:ty),+ $(,)?) => {$(
        impl ::std::fmt::Display for $t {
            fn fmt(&self, f: &mut ::std::fmt::Formatter<'_>) -> ::std::fmt::Result {
                f.write_str(&$crate::names::name(self))
            }
        }
        impl ::std::str::FromStr for $t {
            type Err = String;
            fn from_str(s: &str) -> ::std::result::Result<Self, String> {
                $crate::names::parse(s)
            }
        }
        impl $crate::names::__rusqlite::types::ToSql for $t {
            fn to_sql(&self) -> $crate::names::__rusqlite::Result<$crate::names::__rusqlite::types::ToSqlOutput<'_>> {
                Ok($crate::names::__rusqlite::types::ToSqlOutput::from($crate::names::name(self)))
            }
        }
        impl $crate::names::__rusqlite::types::FromSql for $t {
            fn column_result(
                value: $crate::names::__rusqlite::types::ValueRef<'_>,
            ) -> $crate::names::__rusqlite::types::FromSqlResult<Self> {
                $crate::names::parse(value.as_str()?)
                    .map_err(|e| $crate::names::__rusqlite::types::FromSqlError::Other(e.into()))
            }
        }
    )+};
}

#[cfg(test)]
mod tests {
    use serde::{Deserialize, Serialize};

    #[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
    #[serde(rename_all = "snake_case")]
    enum Color {
        DarkRed,
        Blue,
    }
    crate::serde_names!(Color);

    #[test]
    fn names_follow_serde() {
        assert_eq!(Color::DarkRed.to_string(), "dark_red");
        assert_eq!("blue".parse::<Color>(), Ok(Color::Blue));
        let err = "green".parse::<Color>().unwrap_err();
        assert!(err.contains("dark_red") && err.contains("blue"), "{err}");
        let c = rusqlite::Connection::open_in_memory().unwrap();
        let back: Color = c.query_row("SELECT ?1", [Color::DarkRed], |r| r.get(0)).unwrap();
        assert_eq!(back, Color::DarkRed);
        let bad: rusqlite::Result<Color> = c.query_row("SELECT 'x'", [], |r| r.get(0));
        assert!(bad.is_err());
    }
}

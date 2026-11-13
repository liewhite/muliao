//! 记录的编号与几种规整过的值。
//!
//! 各领域的编号各是一个类型（[`AgentId`]、[`SessionId`]、[`RunId`]、[`MailId`]、[`HoldId`]）：只是名字，不带领域规则，
//! 放在这里因为各层都要传它们（共享内核，docs/architecture.md 第 3 节）。不同编号不能互传，传错位置编译不过。
//! 只在读入边界构造：读库（`FromSql`）、HTTP 路径与请求体（`Deserialize`）、新建记录（`new`）；序列化、存库、
//! 接口描述里都与内部的字符串或整数相同（旧数据、前端无感）。

use std::fmt;

use rusqlite::types::{FromSql, FromSqlError, FromSqlResult, ToSql, ToSqlOutput, ValueRef};
use serde::{Deserialize, Serialize};

use crate::errors::DomainError;

/// 新记录的随机编号：UUID v4 的字符串形式（与 Python 版相同，旧数据的 id 原样可用）。
pub fn new_id() -> String {
    uuid::Uuid::new_v4().to_string()
}

macro_rules! text_id {
    ($(#[$doc:meta])* $name:ident) => {
        $(#[$doc])*
        #[derive(Clone, PartialEq, Eq, Hash, PartialOrd, Ord, Serialize, Deserialize, utoipa::ToSchema)]
        #[serde(transparent)]
        #[schema(value_type = String)]
        pub struct $name(String);

        impl $name {
            /// 从已知是这种编号的文字构造（读入边界用）。
            pub fn new(id: impl Into<String>) -> Self {
                Self(id.into())
            }

            pub fn as_str(&self) -> &str {
                &self.0
            }
        }

        // 按编号作键的表可以用字符串查（与字符串的比较、哈希、排序相同）
        impl std::borrow::Borrow<str> for $name {
            fn borrow(&self) -> &str {
                &self.0
            }
        }

        impl PartialEq<str> for $name {
            fn eq(&self, other: &str) -> bool {
                self.0 == other
            }
        }

        impl PartialEq<&str> for $name {
            fn eq(&self, other: &&str) -> bool {
                self.0 == *other
            }
        }

        impl From<&str> for $name {
            fn from(id: &str) -> Self {
                Self(id.to_owned())
            }
        }

        impl From<String> for $name {
            fn from(id: String) -> Self {
                Self(id)
            }
        }

        // 与内部的值写法相同（报错文字里 `{id:?}` 不带类型名）
        impl fmt::Debug for $name {
            fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
                fmt::Debug::fmt(&self.0, f)
            }
        }

        impl fmt::Display for $name {
            fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
                f.write_str(&self.0)
            }
        }

        impl ToSql for $name {
            fn to_sql(&self) -> rusqlite::Result<ToSqlOutput<'_>> {
                self.0.to_sql()
            }
        }

        impl FromSql for $name {
            fn column_result(v: ValueRef<'_>) -> FromSqlResult<Self> {
                String::column_result(v).map(Self)
            }
        }
    };
}

macro_rules! row_id {
    ($(#[$doc:meta])* $name:ident) => {
        $(#[$doc])*
        #[derive(Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord, Serialize, Deserialize, utoipa::ToSchema)]
        #[serde(transparent)]
        #[schema(value_type = i64)]
        pub struct $name(i64);

        impl $name {
            /// 从库里的行号构造（读入边界用）。
            pub fn new(id: i64) -> Self {
                Self(id)
            }

            pub fn get(self) -> i64 {
                self.0
            }
        }

        impl PartialEq<i64> for $name {
            fn eq(&self, other: &i64) -> bool {
                self.0 == *other
            }
        }

        // 与内部的值写法相同（报错文字里 `{id:?}` 不带类型名）
        impl fmt::Debug for $name {
            fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
                fmt::Debug::fmt(&self.0, f)
            }
        }

        impl fmt::Display for $name {
            fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
                fmt::Display::fmt(&self.0, f)
            }
        }

        impl ToSql for $name {
            fn to_sql(&self) -> rusqlite::Result<ToSqlOutput<'_>> {
                self.0.to_sql()
            }
        }

        impl FromSql for $name {
            fn column_result(v: ValueRef<'_>) -> FromSqlResult<Self> {
                i64::column_result(v).map(Self)
            }
        }
    };
}

text_id!(
    /// agent 的编号（agents 表的 id，也是它资源目录、工作目录的名字）
    AgentId
);
text_id!(
    /// 会话的编号
    SessionId
);
row_id!(
    /// 一次运行的编号（runs 表的行号）
    RunId
);
row_id!(
    /// 一封信件的编号（mail 表的行号）
    MailId
);
row_id!(
    /// 一项占用的编号（session_holds 表的行号）
    HoldId
);

/// 浏览器可以直接显示、模型也普遍能看的图片类型：只有这些按 inline 返回，并作为图片交给模型。SVG 不在其中：它能带脚本
const INLINE_IMAGES: &[&str] = &["image/png", "image/jpeg", "image/gif", "image/webp"];

/// 媒体类型：只存 `type/subtype`（小写）。参数（如 charset）不影响怎么显示和交给模型，下载一律按原始字节。
#[derive(Debug, Clone, PartialEq, Eq, Hash, Serialize, Deserialize, utoipa::ToSchema)]
#[serde(into = "String", try_from = "String")]
#[schema(value_type = String)]
pub struct MediaType(String);

impl MediaType {
    /// 解析 Content-Type 一类的写法：去掉参数、转小写；不是 `type/subtype` 报 InvalidInput。
    pub fn parse(raw: &str) -> Result<Self, DomainError> {
        let essence = raw.split(';').next().unwrap_or_default().trim();
        let valid = match essence.split_once('/') {
            Some((kind, subtype)) => {
                !kind.is_empty() && !subtype.is_empty() && !essence.chars().any(char::is_whitespace)
            }
            None => false,
        };
        if !valid {
            return Err(DomainError::invalid(format!("media type {raw:?} must be 'type/subtype'")));
        }
        Ok(Self(essence.to_lowercase()))
    }

    /// 程序里写死的已知类型（必须已是规整的写法）。
    pub fn known(essence: &'static str) -> Self {
        debug_assert!(Self::parse(essence).is_ok_and(|m| m.0 == essence), "{essence:?} is not normalized");
        Self(essence.to_owned())
    }

    pub fn as_str(&self) -> &str {
        &self.0
    }

    /// 浏览器与模型都能直接显示的图片。
    pub fn is_inline_image(&self) -> bool {
        INLINE_IMAGES.contains(&self.0.as_str())
    }
}

impl TryFrom<String> for MediaType {
    type Error = DomainError;

    fn try_from(raw: String) -> Result<Self, DomainError> {
        Self::parse(&raw)
    }
}

impl From<MediaType> for String {
    fn from(m: MediaType) -> String {
        m.0
    }
}

impl fmt::Display for MediaType {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.0)
    }
}

impl ToSql for MediaType {
    fn to_sql(&self) -> rusqlite::Result<ToSqlOutput<'_>> {
        self.0.to_sql()
    }
}

impl FromSql for MediaType {
    fn column_result(v: ValueRef<'_>) -> FromSqlResult<Self> {
        let raw = String::column_result(v)?;
        Self::parse(&raw).map_err(|e| FromSqlError::Other(Box::new(e)))
    }
}

/// 重要程度：1 日常、2 值得注意、3 重大。
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize, utoipa::ToSchema)]
#[serde(try_from = "i64", into = "i64")]
#[schema(value_type = i64, minimum = 1, maximum = 3)]
pub struct Importance(u8);

impl Importance {
    pub const ROUTINE: Importance = Importance(1);

    pub fn get(self) -> i64 {
        i64::from(self.0)
    }
}

impl TryFrom<i64> for Importance {
    type Error = DomainError;

    fn try_from(n: i64) -> Result<Self, DomainError> {
        match n {
            1..=3 => Ok(Importance(n as u8)),
            _ => Err(DomainError::invalid(format!("importance must be 1, 2 or 3, got {n}"))),
        }
    }
}

impl fmt::Display for Importance {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        fmt::Display::fmt(&self.0, f)
    }
}

impl From<Importance> for i64 {
    fn from(i: Importance) -> i64 {
        i.get()
    }
}

impl ToSql for Importance {
    fn to_sql(&self) -> rusqlite::Result<ToSqlOutput<'_>> {
        Ok(ToSqlOutput::from(self.get()))
    }
}

impl FromSql for Importance {
    fn column_result(v: ValueRef<'_>) -> FromSqlResult<Self> {
        Importance::try_from(i64::column_result(v)?).map_err(|e| FromSqlError::Other(Box::new(e)))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn media_types_keep_only_the_lowercase_essence() {
        assert_eq!(MediaType::parse("Text/Plain; charset=utf-8").unwrap().as_str(), "text/plain");
        for bad in ["text", "/plain", "text/", "te xt/plain", ""] {
            assert!(MediaType::parse(bad).is_err(), "{bad:?}");
        }
        assert!(MediaType::parse("IMAGE/PNG").unwrap().is_inline_image());
        assert!(!MediaType::known("image/svg+xml").is_inline_image());
    }

    #[test]
    fn importance_is_one_to_three() {
        assert!(Importance::try_from(0).is_err());
        assert_eq!(Importance::try_from(3).unwrap().get(), 3);
        assert!(serde_json::from_str::<Importance>("4").is_err());
        assert_eq!(serde_json::to_string(&Importance::ROUTINE).unwrap(), "1");
    }

    #[test]
    fn ids_read_and_print_as_their_value() {
        let id = SessionId::new("s1");
        assert_eq!(format!("{id:?} {id}"), "\"s1\" s1");
        assert_eq!(serde_json::to_string(&RunId::new(7)).unwrap(), "7");
        assert_eq!(serde_json::from_str::<AgentId>("\"a\"").unwrap(), AgentId::new("a"));
    }
}

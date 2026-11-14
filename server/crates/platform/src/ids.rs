//! 记录的编号与几种规整过的值。
//!
//! 各领域的编号各是一个类型（[`AgentId`]、[`SessionId`]、[`RunId`]、[`MailId`]、[`HoldId`]、[`JobId`]、
//! [`DelegationId`]、[`IssueId`]、[`TriggerId`]、[`NotificationId`]、[`MessageId`]、[`GrantId`]、[`FileId`]、
//! [`CallId`]）：只是名字，不带领域规则，
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

/// 工具参数（JSON Schema）里写成内部的值 `$inner`：就地展开、不带本类型的说明（说明由参数字段自己写），模型看到的参数
/// 与用裸类型时逐字相同。
#[macro_export]
macro_rules! json_schema_as {
    ($name:ty, $inner:ty) => {
        impl ::schemars::JsonSchema for $name {
            fn inline_schema() -> bool {
                true
            }

            fn schema_name() -> ::std::borrow::Cow<'static, str> {
                <$inner as ::schemars::JsonSchema>::schema_name()
            }

            fn json_schema(g: &mut ::schemars::SchemaGenerator) -> ::schemars::Schema {
                <$inner as ::schemars::JsonSchema>::json_schema(g)
            }
        }
    };
}

/// 规整过的名字（各领域自己的规则）：`checked_text!(名字类型, 检查函数)`，检查函数是
/// `fn(&str) -> Result<String, DomainError>`，返回规整后的写法。只经 `parse` 构造，反序列化（HTTP、工具参数）与读库也走它；
/// 序列化、存库、接口描述与工具参数的 JSON Schema 里都是普通字符串。使用方要依赖 serde、utoipa、schemars。
#[macro_export]
macro_rules! checked_text {
    ($(#[$doc:meta])* $name:ident, $check:path) => {
        $(#[$doc])*
        #[derive(Clone, PartialEq, Eq, Hash, PartialOrd, Ord, ::serde::Serialize, ::serde::Deserialize, ::utoipa::ToSchema)]
        #[serde(into = "String", try_from = "String")]
        #[schema(value_type = String)]
        pub struct $name(String);

        impl $name {
            pub fn parse(raw: &str) -> ::std::result::Result<Self, $crate::errors::DomainError> {
                $check(raw).map(Self)
            }

            pub fn as_str(&self) -> &str {
                &self.0
            }
        }

        impl ::std::convert::TryFrom<String> for $name {
            type Error = $crate::errors::DomainError;

            fn try_from(raw: String) -> ::std::result::Result<Self, $crate::errors::DomainError> {
                Self::parse(&raw)
            }
        }

        impl ::std::convert::From<$name> for String {
            fn from(n: $name) -> String {
                n.0
            }
        }

        // 与内部的值写法相同（报错文字里 `{name:?}` 不带类型名）
        impl ::std::fmt::Debug for $name {
            fn fmt(&self, f: &mut ::std::fmt::Formatter<'_>) -> ::std::fmt::Result {
                ::std::fmt::Debug::fmt(&self.0, f)
            }
        }

        impl ::std::fmt::Display for $name {
            fn fmt(&self, f: &mut ::std::fmt::Formatter<'_>) -> ::std::fmt::Result {
                f.write_str(&self.0)
            }
        }

        impl $crate::names::__rusqlite::types::ToSql for $name {
            fn to_sql(&self) -> $crate::names::__rusqlite::Result<$crate::names::__rusqlite::types::ToSqlOutput<'_>> {
                $crate::names::__rusqlite::types::ToSql::to_sql(&self.0)
            }
        }

        impl $crate::names::__rusqlite::types::FromSql for $name {
            fn column_result(
                v: $crate::names::__rusqlite::types::ValueRef<'_>,
            ) -> $crate::names::__rusqlite::types::FromSqlResult<Self> {
                Self::parse(v.as_str()?).map_err(|e| $crate::names::__rusqlite::types::FromSqlError::Other(Box::new(e)))
            }
        }

        $crate::json_schema_as!($name, String);
    };
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

        $crate::json_schema_as!($name, String);
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

        $crate::json_schema_as!($name, i64);
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
row_id!(
    /// 一个后台任务的编号（jobs 表的行号）
    JobId
);
row_id!(
    /// 一次委派的编号（thread_calls 表的行号）
    DelegationId
);
row_id!(
    /// issue 的编号（issues 表的行号，看板上的 #N）
    IssueId
);
row_id!(
    /// 触发器的编号（triggers 表的行号）
    TriggerId
);
row_id!(
    /// 通知的编号（notifications 表的行号）
    NotificationId
);
row_id!(
    /// 会话里一条消息的编号（messages 表的行号）
    MessageId
);
row_id!(
    /// 一条「始终允许」的编号（script_grants 表的行号）
    GrantId
);
text_id!(
    /// 上传或交回的文件的编号（files 表的 id）
    FileId
);
text_id!(
    /// 一次工具调用的编号（模型给出，会话里唯一；挂起的调用、委派、提问与审批的回答都按它对应）
    CallId
);

/// 浏览器可以直接显示、模型也普遍能看的图片类型：只有这些按 inline 返回，并作为图片交给模型。SVG 不在其中：它能带脚本
const INLINE_IMAGES: &[&str] = &["image/png", "image/jpeg", "image/gif", "image/webp"];

/// 解析 Content-Type 一类的写法：去掉参数、转小写；不是 `type/subtype` 报 InvalidInput。
fn media_type(raw: &str) -> Result<String, DomainError> {
    let essence = raw.split(';').next().unwrap_or_default().trim();
    let valid = match essence.split_once('/') {
        Some((kind, subtype)) => !kind.is_empty() && !subtype.is_empty() && !essence.chars().any(char::is_whitespace),
        None => false,
    };
    if !valid {
        return Err(DomainError::invalid(format!("media type {raw:?} must be 'type/subtype'")));
    }
    Ok(essence.to_lowercase())
}

crate::checked_text!(
    /// 媒体类型：只存 `type/subtype`（小写）。参数（如 charset）不影响怎么显示和交给模型，下载一律按原始字节。
    MediaType,
    media_type
);

impl MediaType {
    /// 浏览器与模型都能直接显示的图片。
    pub fn is_inline_image(&self) -> bool {
        INLINE_IMAGES.contains(&self.0.as_str())
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
        assert!(!MediaType::parse("image/svg+xml").unwrap().is_inline_image());
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

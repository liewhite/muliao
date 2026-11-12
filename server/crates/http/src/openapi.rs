//! 接口描述的收尾：标题、版本，以及「响应里的每个字段都必有」。
//!
//! 响应用 serde 序列化，每个字段都会出现（值可能是 null；响应类型不用 skip_serializing_if），而 utoipa 把 `Option` 字段描述
//! 成可以不出现。前端按描述生成类型，「可能没有」会逼前端处处多判一次 undefined，所以响应用到的类型统一改成必有（可为
//! null），与 Python 版的描述一致。只出现在请求体里的类型不动：可以不给的字段（`#[serde(default)]`、`Option`）照样可以不给。

use std::collections::BTreeSet;

use utoipa::openapi::schema::{AdditionalProperties, ArrayItems, Schema};
use utoipa::openapi::{OpenApi, RefOr};

const PREFIX: &str = "#/components/schemas/";

pub fn finish(mut doc: OpenApi) -> OpenApi {
    doc.info.title = "Muliao".into();
    doc.info.version = env!("CARGO_PKG_VERSION").into();
    doc.info.description = None;
    doc.info.license = None;
    doc.info.contact = None;
    let Some(components) = doc.components.as_mut() else { return doc };
    // 响应直接引用的类型，再沿引用找全
    let mut pending: Vec<String> = Vec::new();
    for item in doc.paths.paths.values() {
        for op in [&item.get, &item.put, &item.post, &item.delete, &item.patch].into_iter().flatten() {
            for response in op.responses.responses.values() {
                let RefOr::T(response) = response else { continue };
                for content in response.content.values() {
                    if let RefOr::T(content) = content
                        && let Some(schema) = &content.schema
                    {
                        refs_in(schema, &mut pending);
                    }
                }
            }
        }
    }
    let mut seen = BTreeSet::new();
    while let Some(name) = pending.pop() {
        if seen.insert(name.clone())
            && let Some(schema) = components.schemas.get(&name)
        {
            refs_in(schema, &mut pending);
        }
    }
    for name in &seen {
        if let Some(schema) = components.schemas.get_mut(name) {
            require_all(schema);
        }
    }
    doc
}

/// schema 里（含内联的子结构）引用的组件名
fn refs_in(schema: &RefOr<Schema>, out: &mut Vec<String>) {
    let schema = match schema {
        RefOr::Ref(r) => {
            if let Some(name) = r.ref_location.strip_prefix(PREFIX) {
                out.push(name.to_owned());
            }
            return;
        }
        RefOr::T(schema) => schema,
    };
    match schema {
        Schema::Object(o) => {
            o.properties.values().for_each(|p| refs_in(p, out));
            if let Some(AdditionalProperties::RefOr(s)) = o.additional_properties.as_deref() {
                refs_in(s, out);
            }
        }
        Schema::Array(a) => {
            if let ArrayItems::RefOrSchema(item) = &a.items {
                refs_in(item, out);
            }
        }
        Schema::OneOf(o) => o.items.iter().for_each(|s| refs_in(s, out)),
        Schema::AllOf(o) => o.items.iter().for_each(|s| refs_in(s, out)),
        Schema::AnyOf(o) => o.items.iter().for_each(|s| refs_in(s, out)),
        _ => {}
    }
}

/// 对象的每个属性都必有（内联的子结构同样处理；引用的组件各自处理）
fn require_all(schema: &mut RefOr<Schema>) {
    let RefOr::T(schema) = schema else { return };
    match schema {
        Schema::Object(o) => {
            o.required = o.properties.keys().cloned().collect();
            o.properties.values_mut().for_each(require_all);
            if let Some(AdditionalProperties::RefOr(s)) = o.additional_properties.as_deref_mut() {
                require_all(s);
            }
        }
        Schema::Array(a) => {
            if let ArrayItems::RefOrSchema(item) = &mut a.items {
                require_all(item);
            }
        }
        Schema::OneOf(o) => o.items.iter_mut().for_each(require_all),
        Schema::AllOf(o) => o.items.iter_mut().for_each(require_all),
        Schema::AnyOf(o) => o.items.iter_mut().for_each(require_all),
        _ => {}
    }
}

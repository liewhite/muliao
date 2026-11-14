//! 前后端各写一份的常量与路由：前端（web/src）的值必须与后端的唯一来源一致。接口里的类型由 OpenAPI 生成，这里管的是
//! 不在接口类型里的那几个值（文件地址前缀、全局记忆的名字、通知点开的页面路由）。

use muliao_platform::ids::SessionId;
use std::path::PathBuf;

/// web/src/<file> 里 `export const <name> = ` 这一行等号后面的部分
fn definition(file: &str, name: &str) -> String {
    let path = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../../web/src").join(file);
    let source = std::fs::read_to_string(&path).unwrap_or_else(|e| panic!("reading {}: {e}", path.display()));
    let prefix = format!("export const {name} = ");
    let line = source.lines().find_map(|l| l.strip_prefix(&prefix));
    line.unwrap_or_else(|| panic!("{name} not found in web/src/{file}")).to_owned()
}

/// 字符串常量 `'...'` 的值
fn constant(file: &str, name: &str) -> String {
    let value = definition(file, name);
    let quoted = value.strip_prefix('\'').and_then(|v| v.strip_suffix('\''));
    quoted.unwrap_or_else(|| panic!("{name} in web/src/{file} is not a plain string: {value}")).to_owned()
}

/// 前端路由函数 `(id: ...) => `...${id}`` 代入 arg。
fn route(file: &str, name: &str, arg: &str) -> String {
    let value = definition(file, name);
    let template = value.split_once("=> `").and_then(|(_, t)| t.strip_suffix('`'));
    let template = template.unwrap_or_else(|| panic!("{name} in web/src/{file} is not a route template: {value}"));
    template.replace("${id}", arg)
}

#[test]
fn the_committed_openapi_snapshot_matches_the_api() {
    // 前端类型（web/src/schema.d.ts）由它生成。不一致时重新生成：cargo run --bin muliao openapi > ../web/openapi.json，
    // 再在 web/ 里 npm run gen:api
    let path = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../../web/openapi.json");
    let committed: serde_json::Value = serde_json::from_str(&std::fs::read_to_string(&path).unwrap()).unwrap();
    assert!(committed == serde_json::to_value(muliao_http::openapi()).unwrap(), "web/openapi.json is out of date");
}

#[test]
fn frontend_constants_match_the_backend() {
    assert_eq!(constant("api.ts", "FILES_PATH"), muliao_files::FILES_PATH);
}

#[test]
fn notification_urls_are_the_frontend_routes() {
    use muliao_notifications::inbox::url_of;
    assert_eq!(url_of(Some(&SessionId::new("s1")), None).unwrap(), route("api.ts", "sessionRoute", "s1"));
    assert_eq!(muliao_issues::route(7), route("issues.ts", "issueRoute", "7"));
}

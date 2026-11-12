//! 领域 crate 之间的依赖按层单向（docs/architecture.md 2.3）：只能依赖更早一层的。Cargo 只拦循环依赖，同层、
//! 反向的依赖要靠这里查。新领域在 LAYERS 里定层，没定层的 crate 也报错。

use std::collections::BTreeMap;
use std::process::Command;

/// 领域 crate 的层（包名去掉 `muliao-`）。http、组合根在最上面，可以依赖所有领域；迁移程序再上一层，用组合根的表结构与装配
const LAYERS: &[(&str, u32)] = &[
    ("platform", 0),
    ("assets", 1),
    ("files", 1),
    ("usage", 1),
    ("web", 1),
    ("speech", 1),
    ("runtime", 2),
    ("sessions", 3),
    ("workspace", 4),
    ("notifications", 4),
    ("issues", 4),
    ("triggers", 5),
    ("history", 6),
    ("http", 7),
    ("server", 8),
    ("migrate", 9),
];

#[test]
fn domain_crates_only_depend_on_earlier_layers() {
    let out = Command::new(env!("CARGO"))
        .args(["metadata", "--format-version", "1", "--no-deps", "--offline"])
        .current_dir(env!("CARGO_MANIFEST_DIR"))
        .output()
        .unwrap();
    assert!(out.status.success(), "{}", String::from_utf8_lossy(&out.stderr));
    let meta: serde_json::Value = serde_json::from_slice(&out.stdout).unwrap();
    let layers: BTreeMap<&str, u32> = LAYERS.iter().copied().collect();
    let mut problems = Vec::new();
    for package in meta["packages"].as_array().unwrap() {
        let name = package["name"].as_str().unwrap();
        let Some(short) = name.strip_prefix("muliao-") else { continue };
        let Some(&layer) = layers.get(short) else {
            problems.push(format!("{name} has no layer in LAYERS"));
            continue;
        };
        for dep in package["dependencies"].as_array().unwrap() {
            // 测试里用别的领域造数据可以
            if dep["kind"].as_str() == Some("dev") {
                continue;
            }
            let Some(dep_short) = dep["name"].as_str().unwrap().strip_prefix("muliao-") else { continue };
            match layers.get(dep_short) {
                Some(&l) if l < layer => {}
                Some(&l) => problems.push(format!("{name} (layer {layer}) depends on muliao-{dep_short} (layer {l})")),
                None => problems.push(format!("{name} depends on muliao-{dep_short}, which has no layer")),
            }
        }
    }
    assert!(problems.is_empty(), "{problems:#?}");
}

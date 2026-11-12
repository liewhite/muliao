//! 对本机线上数据的只读检查（没有 `~/.muliao/config.toml` 的机器上跳过）：线上配置能被读懂、vault 能解开现有的每个
//! secret。只比对能不能解开，不打印、不比较明文。

use std::path::{Path, PathBuf};

use muliao_platform::config::{DEFAULT_CONFIG_PATH, Settings, expand_home};
use muliao_platform::vault::Vault;

fn secret_files(assets: &Path) -> Vec<PathBuf> {
    let mut found = Vec::new();
    for owner in std::fs::read_dir(assets).unwrap().flatten() {
        let Ok(entries) = std::fs::read_dir(owner.path().join("secrets")) else { continue };
        found.extend(entries.flatten().map(|e| e.path()).filter(|p| p.extension().is_some_and(|x| x == "toml")));
    }
    found.sort();
    found
}

#[test]
fn live_config_loads_and_vault_decrypts_every_secret() {
    let path = expand_home(DEFAULT_CONFIG_PATH);
    if !path.exists() {
        eprintln!("skipped: no {}", path.display());
        return;
    }
    let settings = Settings::load(&path).expect("the live config parses");
    let vault = Vault::new(settings.vault.key.expose()).expect("the live vault key is valid");
    let files = secret_files(&settings.assets_dir);
    assert!(!files.is_empty(), "no secrets under {}", settings.assets_dir.display());
    let mut failed = Vec::new();
    for file in &files {
        let doc: toml::Table = toml::from_str(&std::fs::read_to_string(file).unwrap()).unwrap();
        let ciphertext = doc.get("ciphertext").and_then(|v| v.as_str()).unwrap_or_default();
        match vault.decrypt(ciphertext) {
            Ok(plain) if !plain.is_empty() => {}
            _ => failed.push(file.display().to_string()),
        }
    }
    eprintln!("decrypted {} of {} secrets", files.len() - failed.len(), files.len());
    assert!(failed.is_empty(), "cannot decrypt: {failed:?}");
}

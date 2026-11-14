//! secret：一个文件（`secrets/<KEY>.toml`，在 owner 的资源目录下，格式见 [`crate::formats::secret`]），值以 Fernet 密文
//! 存放（[`muliao_platform::vault`]），主密钥在服务端配置。agent 只能通过 key 引用，没有读取明文的工具。
//!
//! 对外只有不含值的 [`SecretInfo`]，与按使用者解析出的明文（[`Secrets::resolve`]）。引用按使用者能用的 secret 解析
//! （自己的与分发给它的）：agent 运行的命令与它连接的 MCP server 都用该 agent；用户（如界面上新增 MCP 时的探测）能用全部。
//! allow_shell 为假的（#528）不给 agent 自己写的命令（run_shell、触发器的 precheck 与 stream），只给 skill 里封装好的脚本
//! 与 MCP 配置；明文交给哪种由调用方说（[`SecretUse`]）。

use std::collections::BTreeMap;
use std::sync::Arc;

use chrono::{DateTime, Utc};
use muliao_platform::vault::Vault;

use crate::access::{Actor, grants_for, usable, writable};
use crate::formats::mcp::McpConfig;
use crate::formats::secret::{SecretFile, fill, refs, render, validate_key};
use crate::kinds::StoredKind;
use crate::source::AssetSource;
use crate::tree::{Asset, Owner};
use muliao_platform::errors::{Result, invalid, not_found};

/// secret 的明文交给哪种使用方：决定 allow_shell 为假的能不能给。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SecretUse {
    /// agent 自己写的命令（run_shell、触发器的 precheck 与 stream）：allow_shell 为假的不给
    AgentCommand,
    /// 封装好的操作（skill_run 拼出的 skill 脚本调用、MCP 配置）
    Wrapped,
}

/// 一个 secret 的信息，不含值。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SecretInfo {
    pub owner: Owner,
    pub key: String,
    pub description: String,
    pub allow_shell: bool,
    pub updated_at: DateTime<Utc>,
}

fn info((a, file): (&Asset, &SecretFile)) -> SecretInfo {
    SecretInfo {
        owner: a.owner(),
        key: a.name().to_owned(),
        description: file.description.clone(),
        allow_shell: file.allow_shell,
        updated_at: a.updated_at,
    }
}

pub struct Secrets {
    source: Arc<AssetSource>,
    vault: Arc<Vault>,
}

impl Secrets {
    pub fn new(source: Arc<AssetSource>, vault: Arc<Vault>) -> Self {
        Self { source, vault }
    }

    /// 新建（归 owner）或改写（actor 要能改它，owner 不变）这个 secret 的值与描述：加密后写进文件。allow_shell 为 None：
    /// 已有的保持原值，新建的允许（只改值时不把设成不允许的悄悄放开）。返回最终的 allow_shell。
    pub async fn set(
        &self,
        key: &str,
        value: &str,
        description: &str,
        actor: &Actor,
        owner: &Owner,
        allow_shell: Option<bool>,
    ) -> Result<bool> {
        if value.is_empty() {
            return Err(invalid("secret value must not be empty"));
        }
        validate_key(key).map_err(invalid)?;
        let ciphertext = self.vault.encrypt(value);
        let w = self.source.writing().await?;
        let existing = writable::<SecretFile>(&w.snapshot, key, actor)?;
        let allow_shell = allow_shell.unwrap_or_else(|| existing.is_none_or(|(_, f)| f.allow_shell));
        // 已有的也按 owner 重算：正在删除的 agent 名下的不再写
        let owner = existing.map_or(owner.clone(), |(a, _)| a.owner());
        let path = w.snapshot.path_of(StoredKind::Secret, key, &owner)?;
        let text = render(&SecretFile { description: description.to_owned(), ciphertext, allow_shell });
        muliao_platform::fs::write_atomic(&path, text.as_bytes())?;
        // 别的 agent 可能依赖它：记录谁改了哪个 key（不记值）
        tracing::info!(owner = ?owner, key, actor = actor.as_str(), "secret set");
        Ok(allow_shell)
    }

    pub async fn get(&self, key: &str) -> Result<SecretInfo> {
        Ok(info(self.source.snapshot().await?.typed::<SecretFile>(key)?))
    }

    /// MCP 配置里的 `${vault:KEY}` 按 user 能用的 secret 代入（不交给 agent 自己的命令）：代入好的配置与用到的明文（脱敏用）。
    pub async fn fill_mcp(&self, user: &Actor, config: &McpConfig) -> Result<(McpConfig, BTreeMap<String, String>)> {
        let mut keys: Vec<String> = Vec::new();
        for k in config.templates().into_iter().flat_map(refs) {
            if !keys.iter().any(|x| x == k) {
                keys.push(k.to_owned());
            }
        }
        let values = self.resolve(user, &keys, SecretUse::Wrapped).await?;
        Ok((config.map_templates(|t| fill(t, &values)), values))
    }

    /// 取 user 能用的 secret 的明文；任一 key 不能用或解不开即报错。明文要交给 agent 自己写的命令时，allow_shell 为假的也
    /// 报错。
    pub async fn resolve(&self, user: &Actor, keys: &[String], usage: SecretUse) -> Result<BTreeMap<String, String>> {
        if keys.is_empty() {
            return Ok(BTreeMap::new());
        }
        let snapshot = self.source.snapshot().await?;
        let grants = grants_for(&snapshot, user);
        let found: BTreeMap<&str, (&Asset, &SecretFile)> =
            usable::<SecretFile>(&snapshot, &grants).into_iter().map(|(a, f)| (a.name(), (a, f))).collect();
        let missing: Vec<&String> = keys.iter().filter(|k| !found.contains_key(k.as_str())).collect();
        if !missing.is_empty() {
            return Err(not_found(format!(
                "secret(s) {missing:?} not found among the secrets you can use (your own and those given to you by \
                 your roles or your own allow rules)"
            )));
        }
        match usage {
            SecretUse::AgentCommand => {
                let denied: Vec<&String> = keys.iter().filter(|k| !found[k.as_str()].1.allow_shell).collect();
                if !denied.is_empty() {
                    return Err(invalid(format!(
                        "secret(s) {denied:?} are not allowed in shell commands (allow_shell = false): use them through \
                         a skill script that wraps the operation, with skill_run(secrets=[...])"
                    )));
                }
            }
            SecretUse::Wrapped => {}
        }
        let mut values = BTreeMap::new();
        for k in keys {
            let (a, file) = found[k.as_str()];
            let value = self.vault.decrypt(&file.ciphertext).map_err(|_| {
                invalid(format!(
                    "secret {k:?} cannot be decrypted ({} was changed by hand?); set it again with vault_set",
                    a.path.display()
                ))
            })?;
            values.insert(k.clone(), value);
        }
        Ok(values)
    }
}

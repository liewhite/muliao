//! AI 资源文件的写前校验（#500、#578）：写入口（[`crate::source::AssetSource::write`]，界面编辑与文件工具都经它）把要
//! 写成的全文交来，这里按读树的同一套规则校验——布局与名字问 [`place_of`]，各种资源文件的格式按它的
//! [`crate::kinds::KindSpec::vet`]，agent.toml 问 [`agent_file`]，access.toml 问 [`access_file::vet`]——返回实际存下的
//! 全文；不合格就拒绝（InvalidInput），说明原因、这是谁的资源、正确的写法在公共 skill 的哪一节。用 shell 写的不经过
//! 这里，下次读树时照样报出。只校验格式与名字，不管谁能写（那是写沙箱的事）。隐藏文件不加载，不校验。

use std::path::{Path, PathBuf};

use muliao_platform::fs::{RealPath, is_within, real};

use crate::access_file;
use crate::formats::agent_file::{self, AgentFile};
use crate::formats::{Draft, read_lossy};
use crate::kinds::ASSETS_SKILL;
use crate::kinds::{Section, StoredKind};
use crate::tree::{Owner, Place, Snapshot, dir_owner, place_of};
use muliao_platform::errors::{Result, invalid};

/// agent.toml 写的模型在当前配置下能不能运行；不能时的原因（运行端口给，[`crate::source::ModelCheck`]）。
pub(crate) type CheckAgent<'a> = &'a dyn Fn(&AgentFile) -> std::result::Result<(), String>;

/// path 在资源目录 root 里（按真实路径，不含 root 本身）。
pub fn in_tree(root: &Path, path: &Path) -> bool {
    match (real(root), real(path)) {
        (Ok(r), Ok(p)) => p != r && is_within(&r, &p),
        _ => false,
    }
}

/// path（在资源目录里）要写成 text：返回实际要存下的全文（记忆规整过空白，别的原样）；不合格时是 InvalidInput，
/// 说明写给 agent 看（是谁的资源文件、看 assets-files 哪一节），界面上也原样显示。
pub(crate) fn vet(
    snapshot: &Snapshot,
    path: &Path,
    text: &str,
    memory_max_chars: usize,
    check_agent: CheckAgent<'_>,
) -> Result<String> {
    let root = real(&snapshot.root)?;
    let target = real(path)?;
    let rel: Vec<String> = match target.strip_prefix(root.as_path()) {
        Ok(r) if r.components().next().is_some() => r.iter().map(|p| p.to_string_lossy().into_owned()).collect(),
        _ => {
            return Err(invalid(format!("{} is not in the asset tree {}", path.display(), root.display())));
        }
    };
    if rel.iter().any(|p| p.starts_with('.')) {
        return Ok(text.to_owned());
    }
    let head = &rel[0];
    let owner = dir_owner(&snapshot.names, head);
    let whose = match &owner {
        Some(Owner::Platform) => "the platform's".to_owned(),
        Some(Owner::Agent(_)) => format!("agent {head:?}'s"),
        None => "no one's".to_owned(),
    };
    check(snapshot, &root, &rel, owner.as_ref(), text, memory_max_chars, check_agent).map_err(|(reason, section)| {
        invalid(format!(
            "{} ({whose} asset files under {}): {reason}; nothing was written. The layout and file formats are in the \
             {ASSETS_SKILL} skill, section '{}'.",
            path.display(),
            snapshot.root.display(),
            section.title()
        ))
    })
}

type Rejection = (String, Section);

fn check(
    snapshot: &Snapshot,
    root: &RealPath,
    rel: &[String],
    owner: Option<&Owner>,
    text: &str,
    memory_max_chars: usize,
    check_agent: CheckAgent<'_>,
) -> std::result::Result<String, Rejection> {
    let (head, rest) = rel.split_first().expect("a path in the tree");
    if rest.is_empty() {
        return Err(("the root holds only platform/ and agent directories".into(), Section::Layout));
    }
    let Some(owner) = owner else {
        return Err((
            format!("{head}/ is not an existing agent's directory; agent directories are made by create_agent"),
            Section::Layout,
        ));
    };
    if let Owner::Agent(id) = owner
        && snapshot.deleting.contains(id)
    {
        return Err((
            format!("agent {head:?} is being deleted; nothing can be written into its directory"),
            Section::Layout,
        ));
    }
    let place = place_of(rest, owner).map_err(|e| (e.reason.clone(), e.section()))?;

    let draft =
        |kind: StoredKind, name: &str, inner: &[String], named: bool| -> std::result::Result<String, Rejection> {
            let spec = kind.spec();
            let item_rel = &rest[..rest.len() - inner.len()];
            let item: PathBuf = item_rel.iter().fold(root.join(head), |p, part| p.join(part));
            if named && !item.exists() {
                // 新建一项：名字每种资源全局唯一，别处已有的不让建（读树时两处都会被跳过）
                snapshot.check_free(kind, name).map_err(|e| (e.to_string(), spec.section))?;
            }
            (spec.vet)(&Draft { item, name, inner, text, memory_max_chars }).map_err(|e| (e, spec.section))
        };

    match place {
        Place::Config => match owner {
            Owner::Platform => {
                let path = snapshot.access_path();
                let before = if path.is_file() { read_lossy(&path) } else { None };
                access_file::vet(before.as_deref(), text, &snapshot.tools, &snapshot.addressable())
                    .map_err(|e| (e.to_string(), Section::RolesAndPermissions))?;
                let known = snapshot.names.keys().cloned().collect();
                let unknown = access_file::unknown_new_agents(before.as_deref(), text, &known);
                if !unknown.is_empty() {
                    return Err((
                        format!(
                            "[agents.<id>] sections {unknown:?} name no existing agent; the section name is the agent's \
                             id, shown by get_agent"
                        ),
                        Section::RolesAndPermissions,
                    ));
                }
                Ok(text.to_owned())
            }
            Owner::Agent(_) => {
                let data = agent_file::parse(text).map_err(|e| (e, Section::AgentToml))?;
                check_agent(&data).map_err(|e| (e, Section::AgentToml))?;
                Ok(text.to_owned())
            }
        },
        // 放资源的子目录本身总是目录；skill 这类目录资源的条目本身也是目录
        Place::Shelf(kind) => Err((format!("{} is a directory, not a file", rest.join("/")), kind.spec().section)),
        Place::Entry { kind, inner, .. } if kind.spec().is_directory && inner.is_empty() => {
            Err((format!("{} is a directory, not a file", rest.join("/")), kind.spec().section))
        }
        Place::PrivateMemory(id) => draft(StoredKind::Memory, snapshot.names[&id].as_str(), &[], false),
        Place::Entry { kind, name, inner } => draft(kind, &name, &inner, true),
    }
}

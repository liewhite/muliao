//! 测试用的装配：临时目录里的数据库与资源目录树。
#![allow(dead_code)]

use std::path::{Path, PathBuf};
use std::sync::Arc;

use muliao_assets::access::Actor;
use muliao_assets::agents;
use muliao_assets::rules::{ToolAccess, ToolSpec, ToolTable};
use muliao_assets::service::{AgentService, AgentSpec};
use muliao_assets::source::{AssetSource, ModelCheck};
use muliao_platform::db::{Database, migrate};

pub struct World {
    pub dir: tempfile::TempDir,
    pub db: Arc<Database>,
    pub source: Arc<AssetSource>,
    pub service: Arc<AgentService>,
}

impl World {
    pub fn root(&self) -> PathBuf {
        self.dir.path().join("assets")
    }

    pub fn workspaces(&self) -> PathBuf {
        self.dir.path().join("workspaces")
    }

    pub async fn create(&self, name: &str) -> String {
        self.service
            .create(
                AgentSpec { name: name.into(), description: format!("{name} 的描述"), ..Default::default() },
                &Actor::User,
            )
            .await
            .unwrap()
            .id
    }
}

pub fn tools() -> Arc<ToolTable> {
    Arc::new(ToolTable::of(vec![
        ToolSpec { name: "read_file".into(), access: ToolAccess::Read, admin: false },
        ToolSpec { name: "run_shell".into(), access: ToolAccess::Write, admin: false },
        ToolSpec { name: "create_agent".into(), access: ToolAccess::Write, admin: true },
    ]))
}

/// 模型只认 `p:` 开头的
pub fn model_check() -> ModelCheck {
    Arc::new(
        |model: &str, _| if model.starts_with("p:") { Ok(()) } else { Err(format!("unknown provider in {model:?}")) },
    )
}

pub async fn world() -> World {
    let dir = tempfile::tempdir().unwrap();
    let db_path = dir.path().join("t.db");
    migrate(&db_path, &[agents::MIGRATION]).unwrap();
    let db = Arc::new(Database::open(&db_path, Arc::new(|| {})).unwrap());
    let root = dir.path().join("assets");
    std::fs::create_dir_all(root.join("platform/memory")).unwrap();
    std::fs::write(root.join("platform/memory/global.md"), "全局\n").unwrap();
    let source = Arc::new(AssetSource::new(db.clone(), root, 100, model_check(), tools()));
    let service = Arc::new(AgentService::new(source.clone(), dir.path().join("workspaces"), "p:default".into()));
    World { dir, db, source, service }
}

pub fn write(path: &Path, text: &str) {
    std::fs::create_dir_all(path.parent().unwrap()).unwrap();
    std::fs::write(path, text).unwrap();
}

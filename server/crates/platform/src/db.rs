//! SQLite：单写者事务、只读连接、按领域分的迁移。
//!
//! - **写**：所有写事务在一个专用线程上串行执行（[`Database::write`]），换来无丢失更新、无 SQLITE_BUSY。事务是同步闭包，
//!   里面不能 await，所以「事务里不做模型调用、网络请求这类慢事」由编译器保证；调用方被取消（停止、关停）时事务照样在
//!   写线程上做完或回滚，不会被打断一半。
//! - **读**：只读连接（WAL 下与写并行），[`Database::read`]。
//! - **提交之后**：写过表的事务提交后调 `after_write`（组合根据此推 SSE「changed」）；事务里登记的 [`Tx::on_commit`] 在提交后
//!   按登记顺序执行，回滚时丢弃。
//! - **时间**：一律存 UTC 的 `chrono::DateTime<Utc>`（rusqlite 的 chrono 支持，文本形式），不存本地时间。
//! - **迁移**：每个领域一串 SQL（[`Migration`]），各自记版本，启动时在一个事务里全部升到最新（[`migrate`]）。

use std::any::Any;
use std::ops::{Deref, DerefMut};
use std::panic::{AssertUnwindSafe, catch_unwind, resume_unwind};
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex, mpsc};

use rusqlite::{Connection, OpenFlags, Transaction, TransactionBehavior};
use tokio::sync::oneshot;

/// 写过表的事务提交成功后调用：只做不会失败、不会阻塞的事（如发展示事件）
pub type AfterWrite = Arc<dyn Fn() + Send + Sync>;

type Job = Box<dyn FnOnce(&mut Connection) + Send>;
type Committed = Box<dyn FnOnce() + Send>;

/// 一个写事务。解引用为 rusqlite 的 [`Transaction`]（进而是 [`Connection`]），直接执行 SQL。
pub struct Tx<'c> {
    tx: Transaction<'c>,
    committed: Vec<Committed>,
}

impl Tx<'_> {
    /// 登记提交之后要做的事（如杀进程、唤醒会话 actor）：在写线程上、事务提交之后按登记顺序执行，回滚时丢弃。只做不会
    /// 阻塞的事；要等的（网络、进程）用 `tokio::spawn` 放出去（写线程进入了打开数据库时的 tokio 运行时）。
    pub fn on_commit(&mut self, action: impl FnOnce() + Send + 'static) {
        self.committed.push(Box::new(action));
    }
}

impl<'c> Deref for Tx<'c> {
    type Target = Transaction<'c>;
    fn deref(&self) -> &Self::Target {
        &self.tx
    }
}

impl DerefMut for Tx<'_> {
    fn deref_mut(&mut self) -> &mut Self::Target {
        &mut self.tx
    }
}

pub struct Database {
    path: PathBuf,
    writer: mpsc::Sender<Job>,
    readers: Arc<Mutex<Vec<Connection>>>,
    after_write: AfterWrite,
}

fn configure(conn: &Connection) -> rusqlite::Result<()> {
    conn.busy_timeout(std::time::Duration::from_secs(5))?;
    conn.pragma_update(None, "foreign_keys", true)?;
    Ok(())
}

impl Database {
    /// 打开数据库（应已经 [`migrate`] 过），起写线程。必须在 tokio 运行时里调用（否则 panic）：写线程进入这个运行时，
    /// [`Tx::on_commit`] 里可以 `tokio::spawn`。
    pub fn open(path: &Path, after_write: AfterWrite) -> rusqlite::Result<Self> {
        let mut conn = Connection::open(path)?;
        let mode: String = conn.pragma_update_and_check(None, "journal_mode", "WAL", |r| r.get(0))?;
        if !mode.eq_ignore_ascii_case("wal") {
            return Err(rusqlite::Error::InvalidParameterName(format!("journal_mode is {mode}, not WAL")));
        }
        configure(&conn)?;
        let (sender, jobs) = mpsc::channel::<Job>();
        let runtime = tokio::runtime::Handle::current();
        std::thread::Builder::new()
            .name("muliao-db-writer".into())
            .spawn(move || {
                let _entered = runtime.enter();
                // 所有 Database 句柄都 drop 后 recv 失败，线程结束；排在前面的事务照常做完
                while let Ok(job) = jobs.recv() {
                    job(&mut conn);
                }
            })
            .expect("spawn database writer thread");
        Ok(Self { path: path.to_path_buf(), writer: sender, readers: Arc::default(), after_write })
    }

    /// 在写线程上跑一个写事务：f 返回 Ok 提交，Err 回滚；f panic 时回滚并在调用方重新 panic。提交后的 after_write 或
    /// on_commit 动作 panic 时数据已经提交，后面登记的动作不再执行，panic 照样交回调用方。
    ///
    /// 调用方被取消时立即返回，事务在写线程上照常做完（或回滚），之后的写排在它后面。要依据现状做决定的（查了再改），
    /// 查和改都放在同一个闭包里，不要先用 [`Database::read`] 读再来写：两次之间别的事务可能改了它。
    pub async fn write<T, E, F>(&self, f: F) -> Result<T, E>
    where
        F: FnOnce(&mut Tx<'_>) -> Result<T, E> + Send + 'static,
        T: Send + 'static,
        E: From<rusqlite::Error> + Send + 'static,
    {
        let (reply, result) = oneshot::channel::<Result<Result<T, E>, Box<dyn Any + Send>>>();
        let after_write = self.after_write.clone();
        let job: Job = Box::new(move |conn: &mut Connection| {
            // 闭包或提交后的动作 panic 都不让写线程退出：事务随 Transaction 的 drop 回滚，panic 交回调用方
            let outcome = catch_unwind(AssertUnwindSafe(|| -> Result<T, E> {
                let before = conn.total_changes();
                let (value, committed) = transact(conn, f)?;
                // total_changes 也算回滚掉的修改，所以只在提交之后看
                if conn.total_changes() != before {
                    after_write();
                }
                for action in committed {
                    action();
                }
                Ok(value)
            }));
            // 调用方已被取消时没人收：事务照样做完了
            let _ = reply.send(outcome);
        });
        self.writer.send(job).expect("database writer thread is gone");
        match result.await.expect("database writer dropped a transaction") {
            Ok(r) => r,
            Err(panic) => resume_unwind(panic),
        }
    }

    /// 用一个只读连接读（在阻塞线程池里）。写语句会报错。闭包里的所有语句看同一个快照（一个读事务）。
    pub async fn read<T, E, F>(&self, f: F) -> Result<T, E>
    where
        F: FnOnce(&Connection) -> Result<T, E> + Send + 'static,
        T: Send + 'static,
        E: From<rusqlite::Error> + Send + 'static,
    {
        let readers = self.readers.clone();
        let path = self.path.clone();
        tokio::task::spawn_blocking(move || {
            let pooled = readers.lock().expect("reader pool lock").pop();
            let conn = match pooled {
                Some(c) => c,
                None => {
                    let c = Connection::open_with_flags(
                        &path,
                        OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NO_MUTEX | OpenFlags::SQLITE_OPEN_URI,
                    )?;
                    configure(&c)?;
                    c
                }
            };
            // 闭包在一个读事务里跑：WAL 下否则每条语句各取一个快照，两条查询之间的写入会让它们对不上（#766）。
            // 闭包 panic 时连接随之丢弃，不带着未结束的事务回池。
            conn.execute_batch("BEGIN DEFERRED")?;
            let result = f(&conn);
            // 只读事务，结束方式无所谓；结束不了的连接不回池，免得下次接着用旧快照
            if conn.execute_batch("ROLLBACK").is_ok() {
                readers.lock().expect("reader pool lock").push(conn);
            }
            result
        })
        .await
        .unwrap_or_else(|e| resume_unwind(e.into_panic()))
    }
}

/// 在 conn 上跑一个写事务，提交后交回值与登记的提交后动作（还没执行）。
fn transact<T, E: From<rusqlite::Error>>(
    conn: &mut Connection,
    f: impl FnOnce(&mut Tx<'_>) -> Result<T, E>,
) -> Result<(T, Vec<Committed>), E> {
    let mut tx = Tx { tx: conn.transaction_with_behavior(TransactionBehavior::Immediate)?, committed: Vec::new() };
    let value = f(&mut tx)?;
    tx.tx.commit()?;
    Ok((value, tx.committed))
}

/// 测试在 [`Database`] 之外直接打开的库上写（如迁移测试往停在旧版本的库里造数据）：同步跑一个写事务，提交后执行登记的
/// 动作。服务里的写一律经 [`Database::write`]。
#[cfg(feature = "testing")]
pub fn write_on<T, E: From<rusqlite::Error>>(
    conn: &mut Connection,
    f: impl FnOnce(&mut Tx<'_>) -> Result<T, E>,
) -> Result<T, E> {
    let (value, committed) = transact(conn, f)?;
    for action in committed {
        action();
    }
    Ok(value)
}

/// 一个领域的迁移：按顺序的 SQL 脚本（通常 `include_str!` 自领域 crate 的 migrations 目录）。已发布的脚本不改不删，
/// 改结构就往后加一条。版本 = 已执行的条数，按领域记在 `schema_versions` 表。
pub struct Migration {
    pub domain: &'static str,
    pub steps: &'static [&'static str],
}

#[derive(Debug, thiserror::Error)]
pub enum MigrateError {
    #[error(transparent)]
    Sqlite(#[from] rusqlite::Error),
    #[error(
        "domain {domain:?} is at version {found} in the database but the code only knows {known}: the database is newer than this build"
    )]
    Newer { domain: &'static str, found: usize, known: usize },
    #[error("migration {domain:?} step {step} failed: {source}")]
    Step { domain: &'static str, step: usize, source: rusqlite::Error },
    #[error("migration would leave foreign key violations, rolled back: {0:?}")]
    ForeignKeys(Vec<(String, i64, String)>),
}

/// 把数据库升级到各领域的最新结构，全部在一个事务里：任何失败都完整回滚。启动时、打开 [`Database`] 之前调用；已是最新时
/// 什么也不做。
///
/// 迁移连接关掉外键（重建表时「建新表-拷数据-删旧表」，开着外键删旧表会级联删掉子表数据；PRAGMA foreign_keys 在事务里
/// 无效，所以在开事务前关），提交前做一次 foreign_key_check。
pub fn migrate(path: &Path, domains: &[Migration]) -> Result<(), MigrateError> {
    let mut conn = Connection::open(path)?;
    conn.pragma_update(None, "foreign_keys", false)?;
    let tx = conn.transaction_with_behavior(TransactionBehavior::Immediate)?;
    tx.execute_batch(
        "CREATE TABLE IF NOT EXISTS schema_versions (domain TEXT PRIMARY KEY NOT NULL, version INTEGER NOT NULL)",
    )?;
    for m in domains {
        let found: i64 = tx
            .query_row("SELECT version FROM schema_versions WHERE domain = ?1", [m.domain], |r| r.get(0))
            .or_else(|e| if e == rusqlite::Error::QueryReturnedNoRows { Ok(0) } else { Err(e) })?;
        let found = usize::try_from(found).expect("schema version is not negative");
        if found > m.steps.len() {
            return Err(MigrateError::Newer { domain: m.domain, found, known: m.steps.len() });
        }
        for (i, sql) in m.steps.iter().enumerate().skip(found) {
            tx.execute_batch(sql).map_err(|source| MigrateError::Step { domain: m.domain, step: i + 1, source })?;
        }
        if found < m.steps.len() {
            tx.execute(
                "INSERT INTO schema_versions (domain, version) VALUES (?1, ?2)
                 ON CONFLICT (domain) DO UPDATE SET version = excluded.version",
                rusqlite::params![m.domain, m.steps.len() as i64],
            )?;
        }
    }
    let violations: Vec<(String, i64, String)> = {
        let mut stmt = tx.prepare("PRAGMA foreign_key_check")?;
        stmt.query_map([], |r| Ok((r.get(0)?, r.get::<_, Option<i64>>(1)?.unwrap_or(0), r.get(2)?)))?
            .take(10)
            .collect::<Result<_, _>>()?
    };
    if !violations.is_empty() {
        return Err(MigrateError::ForeignKeys(violations));
    }
    tx.commit()?;
    Ok(())
}

/// `IN (…)` 里的 n 个 `?`，逗号分隔。
pub fn placeholders(n: usize) -> String {
    vec!["?"; n].join(", ")
}

/// 含有 text（按字面，`%` `_` `\` 转义）的 LIKE 模式；SQL 里写 `LIKE ? ESCAPE '\'`。
pub fn like_contains(text: &str) -> String {
    format!("%{}%", escape_like(text))
}

/// 以 text 开头（按字面）的 LIKE 模式，同 [`like_contains`]。
pub fn like_prefix(text: &str) -> String {
    format!("{}%", escape_like(text))
}

fn escape_like(text: &str) -> String {
    text.replace('\\', "\\\\").replace('%', "\\%").replace('_', "\\_")
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicUsize, Ordering};

    #[test]
    fn like_patterns_match_literally() {
        assert_eq!(like_contains(r"a%b_c\"), r"%a\%b\_c\\%");
        assert_eq!(like_prefix("ab_"), r"ab\_%");
    }

    const NOTES: Migration = Migration {
        domain: "notes",
        steps: &[
            "CREATE TABLE notes (id INTEGER PRIMARY KEY, body TEXT NOT NULL)",
            "ALTER TABLE notes ADD COLUMN at TEXT",
        ],
    };

    fn setup() -> (tempfile::TempDir, PathBuf) {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("t.db");
        migrate(&path, &[NOTES]).unwrap();
        (dir, path)
    }

    #[test]
    fn migrate_is_idempotent_and_atomic() {
        let (_dir, path) = setup();
        migrate(&path, &[NOTES]).unwrap();
        const BAD: Migration = Migration { domain: "bad", steps: &["CREATE TABLE ok (x)", "NOT SQL"] };
        const MORE: Migration = Migration { domain: "more", steps: &["CREATE TABLE more (x)"] };
        let err = migrate(&path, &[NOTES, MORE, BAD]).unwrap_err();
        assert!(matches!(err, MigrateError::Step { domain: "bad", step: 2, .. }), "{err}");
        let conn = Connection::open(&path).unwrap();
        let tables: i64 = conn
            .query_row("SELECT count(*) FROM sqlite_master WHERE name IN ('ok', 'more')", [], |r| r.get(0))
            .unwrap();
        assert_eq!(tables, 0, "a failed migration leaves nothing behind");
        const OLD: Migration = Migration { domain: "notes", steps: &["CREATE TABLE notes (id INTEGER PRIMARY KEY)"] };
        assert!(matches!(migrate(&path, &[OLD]), Err(MigrateError::Newer { .. })));
    }

    #[tokio::test(flavor = "multi_thread")]
    async fn writes_commit_rollback_and_notify() {
        let (_dir, path) = setup();
        let writes = Arc::new(AtomicUsize::new(0));
        let counter = writes.clone();
        let db = Database::open(
            &path,
            Arc::new(move || {
                counter.fetch_add(1, Ordering::SeqCst);
            }),
        )
        .unwrap();

        let committed = Arc::new(AtomicUsize::new(0));
        let c = committed.clone();
        db.write(move |tx| {
            tx.execute("INSERT INTO notes (body) VALUES ('a')", [])?;
            tx.on_commit(move || {
                c.fetch_add(1, Ordering::SeqCst);
            });
            Ok::<_, rusqlite::Error>(())
        })
        .await
        .unwrap();
        assert_eq!(committed.load(Ordering::SeqCst), 1);
        assert_eq!(writes.load(Ordering::SeqCst), 1);

        // 回滚：on_commit 不执行、不算写
        let c = committed.clone();
        let r: Result<(), rusqlite::Error> = db
            .write(move |tx| {
                tx.execute("INSERT INTO notes (body) VALUES ('b')", [])?;
                tx.on_commit(move || {
                    c.fetch_add(1, Ordering::SeqCst);
                });
                Err(rusqlite::Error::QueryReturnedNoRows)
            })
            .await;
        assert!(r.is_err());
        assert_eq!(committed.load(Ordering::SeqCst), 1);
        assert_eq!(writes.load(Ordering::SeqCst), 1);

        // 只读的事务不算写
        db.write(|tx| tx.query_row("SELECT count(*) FROM notes", [], |r| r.get::<_, i64>(0))).await.unwrap();
        assert_eq!(writes.load(Ordering::SeqCst), 1);

        let n: i64 = db.read(|c| c.query_row("SELECT count(*) FROM notes", [], |r| r.get(0))).await.unwrap();
        assert_eq!(n, 1);
        // 读连接写不了
        assert!(db.read(|c| c.execute("DELETE FROM notes", [])).await.is_err());
    }

    #[tokio::test(flavor = "multi_thread")]
    async fn read_sees_one_snapshot() {
        let (_dir, path) = setup();
        let db = Database::open(&path, Arc::new(|| {})).unwrap();
        let p = path.clone();
        let (before, after): (i64, i64) = db
            .read(move |c| {
                let count = || c.query_row("SELECT count(*) FROM notes", [], |r| r.get(0));
                let before = count()?;
                // 两条查询之间别的连接写入并提交（#766：会话列表的两条查询就这样对不上）
                Connection::open(&p)?.execute("INSERT INTO notes (body) VALUES ('x')", [])?;
                Ok::<_, rusqlite::Error>((before, count()?))
            })
            .await
            .unwrap();
        assert_eq!((before, after), (0, 0));
        // 下一次读（同一个回池的连接）看到新数据
        let n: i64 = db.read(|c| c.query_row("SELECT count(*) FROM notes", [], |r| r.get(0))).await.unwrap();
        assert_eq!(n, 1);
    }

    #[tokio::test(flavor = "multi_thread")]
    async fn panic_rolls_back_and_writer_survives() {
        let (_dir, path) = setup();
        let db = Arc::new(Database::open(&path, Arc::new(|| {})).unwrap());
        let d = db.clone();
        let joined = tokio::spawn(async move {
            d.write(|tx| -> Result<(), rusqlite::Error> {
                tx.execute("INSERT INTO notes (body) VALUES ('x')", [])?;
                panic!("boom");
            })
            .await
        })
        .await;
        assert!(joined.unwrap_err().is_panic());
        let n: i64 = db.write(|tx| tx.query_row("SELECT count(*) FROM notes", [], |r| r.get(0))).await.unwrap();
        assert_eq!(n, 0);
    }

    #[tokio::test(flavor = "multi_thread")]
    async fn cancelled_caller_does_not_interrupt_the_transaction() {
        let (_dir, path) = setup();
        let db = Arc::new(Database::open(&path, Arc::new(|| {})).unwrap());
        let (started_tx, started) = std::sync::mpsc::channel();
        let d = db.clone();
        let task = tokio::spawn(async move {
            d.write(move |tx| {
                started_tx.send(()).unwrap();
                std::thread::sleep(std::time::Duration::from_millis(200));
                tx.execute("INSERT INTO notes (body) VALUES ('late')", [])
            })
            .await
        });
        tokio::task::spawn_blocking(move || started.recv().unwrap()).await.unwrap();
        task.abort();
        let n: i64 = db.write(|tx| tx.query_row("SELECT count(*) FROM notes", [], |r| r.get(0))).await.unwrap();
        assert_eq!(n, 1, "the transaction finished although its caller was cancelled");
    }
}

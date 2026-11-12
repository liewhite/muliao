//! 看板的测试：真库（agents、sessions、issues 的表），来信经会话领域真的收信入口（信进信箱，不跑 actor）；会话事件像会话
//! 领域那样在一个事务里发布给订阅者。

use std::sync::{Arc, Mutex};

use muliao_platform::db::{Database, migrate};
use muliao_sessions::actor::Waker;
use muliao_sessions::events::{RunEnded, RunFailed, SessionDeleted, Subscribers};
use muliao_sessions::inbound::SenderInbox;
use muliao_sessions::mailbox::queued_in;
use muliao_sessions::origins::SessionOrigin;
use muliao_sessions::tools::Stopper;

use crate::activity::Happening;
use crate::graph::Upstream;
use crate::*;

const A: &str = "aaaaaaaaaaaa";
const B: &str = "bbbbbbbbbbbb";
/// 已归档
const C: &str = "cccccccccccc";

struct World {
    _dir: tempfile::TempDir,
    db: Arc<Database>,
    board: Board,
    notices: Arc<Mutex<Vec<IssueNotice>>>,
    subscribers: Subscribers,
    _wakes: tokio::sync::mpsc::UnboundedReceiver<String>,
}

async fn world() -> World {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("t.db");
    migrate(&path, &[muliao_assets::agents::MIGRATION, muliao_files::MIGRATION, muliao_sessions::MIGRATION, MIGRATION])
        .unwrap();
    let db = Arc::new(Database::open(&path, Arc::new(|| {})).unwrap());
    db.write(|tx| {
        for (id, name, archived) in [(A, "alpha", false), (B, "beta", false), (C, "gamma", true)] {
            tx.execute(
                "INSERT INTO agents (id, name, is_default, archived_at, created_at, updated_at) VALUES (?1, ?2, 0, ?3, ?4, ?4)",
                rusqlite::params![id, name, archived.then(chrono::Utc::now), chrono::Utc::now()],
            )?;
        }
        Ok::<_, rusqlite::Error>(())
    })
    .await
    .unwrap();
    let (waker, wakes) = Waker::channel();
    let notices = Arc::new(Mutex::new(Vec::new()));
    let n = notices.clone();
    let notify: NotifyIn = Arc::new(move |_tx, notice| {
        n.lock().unwrap().push(notice);
        Ok(())
    });
    let board = Board::new(db.clone(), SenderInbox::new(waker), notify, Stopper::default(), chrono_tz::Asia::Shanghai);
    let mut subscribers = Subscribers::default();
    board.subscribe(&mut subscribers);
    World { _dir: dir, db, board, notices, subscribers, _wakes: wakes }
}

fn new(owner: &str, title: &str, status: Status, assignee: Option<Assignee>) -> NewIssue {
    NewIssue { owner: owner.into(), title: title.into(), status, assignee, ..NewIssue::default() }
}

fn agent(id: &str) -> Option<Assignee> {
    Some(Assignee::Agent(id.into()))
}

impl World {
    async fn create(&self, by: By, item: NewIssue) -> Issue {
        self.board.create_many(by, vec![item]).await.unwrap().remove(0)
    }

    /// agent 在这个 issue 上的会话
    async fn session(&self, issue_id: i64, agent_id: &str) -> Option<String> {
        let a = agent_id.to_owned();
        self.db.read(move |c| crate::conversation::session_in(c, issue_id, &a)).await.unwrap().map(|s| s.id)
    }

    /// agent 在这个 issue 会话里排着的信
    async fn letters(&self, issue_id: i64, agent_id: &str) -> Vec<String> {
        let Some(s) = self.session(issue_id, agent_id).await else { return vec![] };
        self.db.read(move |c| queued_in(c, &s)).await.unwrap().into_iter().map(|m| m.content).collect()
    }

    /// 信都被处理过了（会话空闲）
    async fn consume_all(&self) {
        self.db.write(|tx| tx.execute("UPDATE mail SET state = 'consumed'", []).map(drop)).await.unwrap();
    }

    async fn timeline(&self, issue_id: i64) -> Vec<activity::Activity> {
        self.board.detail(issue_id).await.unwrap().activities
    }

    async fn publish<E: Send + 'static>(&self, pick: fn(&Subscribers) -> Vec<HandlerOf<E>>, e: E) {
        let handlers = pick(&self.subscribers);
        self.db
            .write(move |tx| -> std::result::Result<(), rusqlite::Error> {
                for h in &handlers {
                    h(tx, &e).unwrap();
                }
                Ok(())
            })
            .await
            .unwrap();
    }
}

type HandlerOf<E> = Arc<
    dyn Fn(&mut muliao_platform::db::Tx<'_>, &E) -> std::result::Result<(), muliao_sessions::events::BoxError>
        + Send
        + Sync,
>;

fn run_ended(w: &Subscribers) -> Vec<HandlerOf<RunEnded>> {
    w.run_ended.clone()
}
fn run_failed(w: &Subscribers) -> Vec<HandlerOf<RunFailed>> {
    w.run_failed.clone()
}
fn session_deleted(w: &Subscribers) -> Vec<HandlerOf<SessionDeleted>> {
    w.session_deleted.clone()
}

fn kinds(t: &[activity::Activity]) -> Vec<&'static str> {
    t.iter().map(|a| a.what.kind()).collect()
}

#[tokio::test]
async fn work_for_an_agent_is_delivered_into_its_issue_session_but_never_back_into_it() {
    let w = world().await;
    let backlog = w.create(By::user(), new(A, "以后再做", Status::Backlog, agent(A))).await;
    assert!(w.session(backlog.id, A).await.is_none(), "backlog is not delivered");
    let i = w.create(By::user(), new(A, "现在做", Status::InProgress, agent(A))).await;
    let letters = w.letters(i.id, A).await;
    assert_eq!(letters.len(), 1);
    assert!(
        letters[0]
            .starts_with(&format!("New activity on issue #{}: 现在做 | status: in_progress | assignee: alpha", i.id))
    );
    assert!(letters[0].contains("user created the issue"), "{}", letters[0]);
    assert!(letters[0].ends_with(&format!("`issue_show({})`.", i.id)));
    let its = w.session(i.id, A).await.unwrap();
    // 在自己这个 issue 会话里评论：不投回
    w.board.comment(By::agent(A, &its), i.id, "进展".into()).await.unwrap();
    assert_eq!(w.letters(i.id, A).await.len(), 1);
    // 在别的会话里：照常投进 issue 会话
    w.board.comment(By::agent(A, "elsewhere"), i.id, "从别处".into()).await.unwrap();
    assert_eq!(w.letters(i.id, A).await.len(), 2);
    // 一次改几个字段只投一封，每个字段一条动态
    let patch = Patch { title: Some("新标题".into()), priority: Some(Priority::High), ..Patch::default() };
    w.board.update(By::user(), i.id, patch).await.unwrap();
    let letters = w.letters(i.id, A).await;
    assert_eq!(letters.len(), 3);
    assert!(
        letters[2].contains("changed title: 现在做 -> 新标题") && letters[2].contains("changed priority: none -> high")
    );
    // 什么都没变：不记、不投
    w.board.update(By::user(), i.id, Patch { priority: Some(Priority::High), ..Patch::default() }).await.unwrap();
    assert_eq!(w.letters(i.id, A).await.len(), 3);
    assert_eq!(kinds(&w.timeline(i.id).await), ["created", "comment", "comment", "title", "priority"]);
    let t = w.timeline(i.id).await;
    assert_eq!(t[1].session_id.as_deref(), Some(its.as_str()), "the activity remembers where it happened");
}

#[tokio::test]
async fn a_user_comment_wakes_the_owner_when_nobody_works_on_the_issue() {
    let w = world().await;
    let i = w.create(By::user(), new(B, "等着", Status::Backlog, None)).await;
    w.board.comment(By::agent(A, "s"), i.id, "agent 说的".into()).await.unwrap();
    assert!(w.letters(i.id, B).await.is_empty(), "an agent's comment does not wake the owner");
    w.board.comment(By::user(), i.id, "用户说的".into()).await.unwrap();
    let letters = w.letters(i.id, B).await;
    assert_eq!(letters.len(), 1);
    assert!(letters[0].contains("user commented:\n  用户说的"), "{}", letters[0]);
    assert!(w.board.comment(By::user(), i.id, "  ".into()).await.is_err());
}

#[tokio::test]
async fn dependencies_hold_back_delivery_until_the_last_upstream_finishes() {
    let w = world().await;
    let step = |key: &str, deps: Vec<Upstream>| NewIssue {
        key: Some(key.into()),
        depends_on: deps,
        ..new(A, key, Status::InProgress, agent(A))
    };
    let created = w
        .board
        .create_many(
            By::agent(A, "chat"),
            vec![
                step("one", vec![]),
                step("two", vec![]),
                step("parent", vec![Upstream::Key("one".into()), Upstream::Key("two".into())]),
            ],
        )
        .await
        .unwrap();
    let (one, two, parent) = (created[0].id, created[1].id, created[2].id);
    assert_eq!(w.letters(one, A).await.len(), 1);
    assert!(w.session(parent, A).await.is_none(), "the parent waits for its steps");
    let work = w.board.get(parent).await.unwrap().work;
    assert_eq!(work.waiting_on, vec![one, two]);
    assert!(!work.stalled, "waiting on dependencies is not stalled");
    w.board.update(By::agent(A, "x"), one, Patch { status: Some(Status::Done), ..Patch::default() }).await.unwrap();
    assert!(w.session(parent, A).await.is_none(), "still waiting on #two");
    let pt = w.timeline(parent).await;
    assert!(matches!(&pt.last().unwrap().what, Happening::Dependency { change } if change.issue == one));
    // 取消也放行
    w.board
        .update(By::agent(A, "x"), two, Patch { status: Some(Status::Cancelled), ..Patch::default() })
        .await
        .unwrap();
    let letters = w.letters(parent, A).await;
    assert_eq!(letters.len(), 1, "the last upstream finishing starts the parent");
    assert!(
        letters[0].contains(&format!("changed status of dependency #{two}: in_progress -> cancelled")),
        "{}",
        letters[0]
    );
    assert!(letters[0].contains(&format!("depends on: #{one} (done), #{two} (cancelled)")), "{}", letters[0]);
    // 成环当场拒绝，带路径
    let err = w
        .board
        .update(By::user(), one, Patch { depends_on: Some(vec![parent]), ..Patch::default() })
        .await
        .unwrap_err();
    assert_eq!(
        err.to_string(),
        format!("dependencies must not form a cycle: #{one} -> #{parent} -> #{one} (each depends on the next)")
    );
    // 一项不合法，整批不建
    let before = w.board.list(Filter::default()).await.unwrap().len();
    let err = w
        .board
        .create_many(By::user(), vec![new(A, "好的", Status::Backlog, None), new(A, " ", Status::Backlog, None)])
        .await
        .unwrap_err();
    assert_eq!(err.to_string(), "issues[1]: issue title must not be empty");
    let err = w
        .board
        .create_many(
            By::user(),
            vec![NewIssue { depends_on: vec![Upstream::Issue(999)], ..new(A, "x", Status::Backlog, None) }],
        )
        .await
        .unwrap_err();
    assert!(err.to_string().contains("cannot depend on #999: not found"), "{err}");
    assert_eq!(w.board.list(Filter::default()).await.unwrap().len(), before);
}

#[tokio::test]
async fn handing_to_the_user_notifies_and_the_user_approves_or_sends_back() {
    let w = world().await;
    let i = w.create(By::user(), new(A, "交审", Status::InProgress, agent(A))).await;
    let its = w.session(i.id, A).await.unwrap();
    let hand = Patch { status: Some(Status::InReview), assignee: Some(Some(Assignee::User)), ..Patch::default() };
    w.board.update(By::agent(A, &its), i.id, hand.clone()).await.unwrap();
    let notices = w.notices.lock().unwrap().clone();
    assert_eq!(notices.len(), 1);
    assert_eq!(notices[0].title, format!("#{} 指派给你：交审", i.id));
    assert_eq!(notices[0].body, "状态 in_review");
    assert_eq!(notices[0].session_id.as_deref(), Some(its.as_str()));
    assert!(w.board.get(i.id).await.unwrap().work.reviewable);
    // 打回：回到交出它的 agent，三条动态一封信
    w.consume_all().await;
    w.board.reject(i.id, "再改改".into()).await.unwrap();
    let letters = w.letters(i.id, A).await;
    assert_eq!(letters.len(), 1);
    assert!(
        letters[0].contains("user commented:\n  再改改")
            && letters[0].contains("changed status: in_review -> in_progress")
            && letters[0].contains("changed assignee: user -> alpha"),
        "{}",
        letters[0]
    );
    // 已不在用户手里：审核冲突
    let err = w.board.approve(i.id).await.unwrap_err();
    assert!(err.to_string().contains("is not assigned to you"), "{err}");
    w.board.update(By::agent(A, &its), i.id, hand).await.unwrap();
    let done = w.board.approve(i.id).await.unwrap();
    assert_eq!(done.status, Status::Done);
    assert!(w.board.approve(i.id).await.unwrap_err().to_string().contains("already done"));
    // 用户自己指派给自己的，打回时没有交出它的 agent
    let mine = w.create(By::user(), new(A, "我的", Status::InReview, Some(Assignee::User))).await;
    assert!(
        w.board.reject(mine.id, "x".into()).await.unwrap_err().to_string().contains("not handed to you by an agent")
    );
    assert_eq!(w.notices.lock().unwrap().len(), 2, "the user assigning to themself is not notified");
}

#[tokio::test]
async fn starting_a_backlog_issue_assigns_its_owner() {
    let w = world().await;
    let i = w.create(By::user(), new(B, "开工", Status::Backlog, None)).await;
    assert!(w.board.get(i.id).await.unwrap().work.startable);
    let started = w.board.start(i.id).await.unwrap();
    assert_eq!((started.status, started.assignee), (Status::InProgress, agent(B)));
    assert_eq!(w.letters(i.id, B).await.len(), 1);
    assert!(w.board.start(i.id).await.unwrap_err().to_string().contains("not in the backlog"));
}

#[tokio::test]
async fn archived_agents_get_nothing_and_the_user_is_told_about_their_own_actions() {
    let w = world().await;
    // 归档的不能新指派
    let err = w.board.create_many(By::user(), vec![new(A, "x", Status::InProgress, agent(C))]).await.unwrap_err();
    assert!(err.to_string().contains("archived"), "{err}");
    let i = w.create(By::user(), new(A, "后来归档", Status::Backlog, agent(B))).await;
    w.db.write(move |tx| {
        tx.execute("UPDATE agents SET archived_at = '2026-01-01T00:00:00Z' WHERE id = ?1", [B]).map(drop)
    })
    .await
    .unwrap();
    w.db.write(move |tx| tx.execute("UPDATE issues SET status = 'in_progress' WHERE id = ?1", [i.id]).map(drop))
        .await
        .unwrap();
    w.board.comment(By::agent(A, "s"), i.id, "agent 的".into()).await.unwrap();
    assert!(w.notices.lock().unwrap().is_empty(), "others' activity on an archived agent is only logged");
    w.board.comment(By::user(), i.id, "用户的".into()).await.unwrap();
    let notices = w.notices.lock().unwrap().clone();
    assert_eq!(notices.len(), 1);
    assert_eq!(notices[0].title, format!("#{} 的动态没交给 beta：它已归档", i.id));
    assert_eq!(kinds(&w.timeline(i.id).await), ["created", "comment", "comment"], "activity is still recorded");
}

#[tokio::test]
async fn a_run_ending_with_the_issue_unsettled_asks_the_agent_to_settle_it() {
    let w = world().await;
    let i = w.create(By::user(), new(A, "做完要表态", Status::InProgress, agent(A))).await;
    let its = w.session(i.id, A).await.unwrap();
    let ended = |stopped: bool, waiting_on| RunEnded {
        session_id: its.clone(),
        session_title: "t".into(),
        origin: SessionOrigin::Issue { issue_id: i.id },
        agent_id: A.into(),
        agent_name: "alpha".into(),
        run_id: 1,
        answer: "好了".into(),
        stopped,
        waiting_on,
        awaited: false,
    };
    // 还有信没处理（会话 running）：不问
    w.publish(run_ended, ended(false, vec![])).await;
    assert_eq!(kinds(&w.timeline(i.id).await), ["created"]);
    w.consume_all().await;
    // 被停止的、停在挂起调用上的：不问
    w.publish(run_ended, ended(true, vec![])).await;
    w.publish(run_ended, ended(false, vec![muliao_sessions::answers::Waiting::AskUser])).await;
    assert_eq!(kinds(&w.timeline(i.id).await), ["created"]);
    w.publish(run_ended, ended(false, vec![])).await;
    let t = w.timeline(i.id).await;
    assert_eq!(kinds(&t), ["created", "unsettled"]);
    assert_eq!(t[1].actor, Actor::Agent(A.into()));
    let letters = w.letters(i.id, A).await;
    assert_eq!(letters.len(), 1, "delivered into its own session although recorded there");
    assert!(letters[0].contains("a comment alone does not settle it"), "{}", letters[0]);
    // 交出去了就不再问
    w.consume_all().await;
    w.board
        .update(By::agent(A, &its), i.id, Patch { status: Some(Status::Blocked), ..Patch::default() })
        .await
        .unwrap();
    w.publish(run_ended, ended(false, vec![])).await;
    assert_eq!(kinds(&w.timeline(i.id).await), ["created", "unsettled", "status"]);
}

#[tokio::test]
async fn failures_deleted_sessions_and_deleted_agents_are_recorded_without_delivery() {
    let w = world().await;
    let i = w.create(By::user(), new(A, "会失败", Status::InProgress, agent(A))).await;
    let its = w.session(i.id, A).await.unwrap();
    w.consume_all().await;
    w.board.comment(By::agent(A, &its), i.id, "在做".into()).await.unwrap();
    w.publish(
        run_failed,
        RunFailed {
            session_id: its.clone(),
            agent_id: A.into(),
            agent_name: "alpha".into(),
            run_id: 2,
            origin: SessionOrigin::Issue { issue_id: i.id },
            error: format!("  {}  ", "坏".repeat(600)),
        },
    )
    .await;
    let t = w.timeline(i.id).await;
    let Happening::RunFailed { body } = &t.last().unwrap().what else { panic!("{t:?}") };
    assert!(body.starts_with("坏坏") && body.contains("[truncated: 100 more chars]"), "{body}");
    assert!(w.letters(i.id, A).await.is_empty(), "a failure is not delivered (it would retry forever)");
    // 会话删除：时间线不再指向它
    w.publish(session_deleted, SessionDeleted { session_id: its.clone(), agent_id: A.into() }).await;
    assert!(w.timeline(i.id).await.iter().all(|a| a.session_id.is_none()));
    // 删除 agent：指派人、负责人置空，以系统身份记
    let hook = w.board.deletion_hook();
    w.db.write(move |tx| hook(tx, A)).await.unwrap();
    let after = w.board.get(i.id).await.unwrap().issue;
    assert_eq!((after.owner, after.assignee), (None, None));
    let t = w.timeline(i.id).await;
    let n = t.len();
    assert_eq!(kinds(&t[n - 2..]), ["assignee", "owner"]);
    assert!(t[n - 2..].iter().all(|a| a.actor == Actor::System));
}

#[tokio::test]
async fn deleting_an_issue_releases_its_downstream() {
    let w = world().await;
    let up = w.create(By::user(), new(A, "上游", Status::InProgress, None)).await;
    let down = w
        .create(
            By::user(),
            NewIssue { depends_on: vec![Upstream::Issue(up.id)], ..new(A, "下游", Status::InProgress, agent(A)) },
        )
        .await;
    assert!(w.session(down.id, A).await.is_none());
    w.board.delete(up.id).await.unwrap();
    let letters = w.letters(down.id, A).await;
    assert_eq!(letters.len(), 1);
    assert!(letters[0].contains(&format!("changed depends_on: #{} -> none", up.id)), "{}", letters[0]);
    assert!(w.board.get(up.id).await.unwrap_err().to_string().contains("not found"));
}

#[tokio::test]
async fn issue_show_lists_and_stop_rules() {
    let w = world().await;
    let up = w.create(By::user(), new(A, "上游", Status::Done, None)).await;
    let i = w
        .create(
            By::agent(A, "chat"),
            NewIssue {
                description: "说明\n两行".into(),
                depends_on: vec![Upstream::Issue(up.id)],
                ..new(A, "看看", Status::InProgress, agent(B))
            },
        )
        .await;
    let text = w.board.render(i.id).await.unwrap();
    let lines: Vec<&str> = text.lines().collect();
    assert_eq!(lines[0], format!("# Issue #{}: 看看", i.id));
    assert_eq!(
        lines[1],
        "owner (project): alpha | status: in_progress | priority: none | assignee: beta | created by: alpha"
    );
    assert_eq!(lines[2], format!("depends on: #{} (done) | waiting on: none | blocks: none", up.id));
    assert_eq!(&lines[3..7], ["", "说明", "两行", ""]);
    assert_eq!(lines[7], "## Activity");
    assert!(lines[8].ends_with("] alpha created the issue") && lines[8].starts_with("- [20"), "{}", lines[8]);
    // 列表：筛选与停滞
    let found = w.board.list(Filter { statuses: vec![Status::InProgress], ..Filter::default() }).await.unwrap();
    assert_eq!(found.iter().map(|b| b.issue.id).collect::<Vec<_>>(), [i.id]);
    assert!(!found[0].work.stalled, "its session has mail to run");
    assert_eq!(found[0].work.sessions.keys().collect::<Vec<_>>(), [B]);
    w.consume_all().await;
    assert!(w.board.get(i.id).await.unwrap().work.stalled);
    let json = serde_json::to_value(w.board.get(i.id).await.unwrap()).unwrap();
    assert_eq!(
        (json["assignee"].as_str(), json["status"].as_str(), json["assignee_state"].as_str()),
        (Some(B), Some("in_progress"), Some("idle"))
    );
    let by_owner = w.board.list(Filter { owner: Some(B.into()), ..Filter::default() }).await.unwrap();
    assert!(by_owner.is_empty());
    // issue_stop 的规则（停会话本身在端到端测试里）
    let err = w.board.stop(By::agent(A, "x"), i.id, Status::Done, None).await.unwrap_err();
    assert!(err.to_string().contains("stopping is not finishing"), "{err}");
    let err = w.board.stop(By::agent(A, "x"), up.id, Status::Blocked, None).await.unwrap_err();
    assert!(err.to_string().contains("not assigned to an agent"), "{err}");
    let its = w.session(i.id, B).await.unwrap();
    let err = w.board.stop(By::agent(B, &its), i.id, Status::Blocked, None).await.unwrap_err();
    assert!(err.to_string().contains("itself"), "{err}");
    // 指派人还没有这个会话：只改状态
    let fresh = w.create(By::user(), new(A, "没会话", Status::Blocked, agent(B))).await;
    let s = w.board.stop(By::agent(A, "x"), fresh.id, Status::Cancelled, None).await.unwrap();
    assert!(!s.stopped);
    assert_eq!(s.issue.status, Status::Cancelled);
}

#[tokio::test]
async fn user_actions_are_grouped_by_issue_for_the_activity_overview() {
    let w = world().await;
    let start = chrono::Utc::now();
    let a = w.create(By::user(), new(A, "甲", Status::Backlog, None)).await;
    let b = w.create(By::user(), new(B, "乙", Status::Backlog, None)).await;
    w.board.comment(By::agent(A, "s"), a.id, "agent 的不算".into()).await.unwrap();
    w.board.comment(By::user(), a.id, "第一句".into()).await.unwrap();
    let end = chrono::Utc::now() + chrono::Duration::seconds(1);
    let all = w.db.read(move |c| user_actions_in(c, start, end, None, chrono_tz::UTC)).await.unwrap();
    assert_eq!(all.iter().map(|u| u.issue_id).collect::<Vec<_>>(), [a.id, b.id]);
    assert_eq!(all[0].lines.len(), 2);
    assert!(all[0].lines[1].contains("user commented:\n  第一句"));
    let only_b =
        w.db.read(move |c| user_actions_in(c, start, end, Some(&[B.to_owned()]), chrono_tz::UTC)).await.unwrap();
    assert_eq!(only_b.iter().map(|u| u.issue_id).collect::<Vec<_>>(), [b.id]);
}

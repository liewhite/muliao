use super::*;
use chrono::TimeZone;
use muliao_platform::ids::{AgentId, SessionId};

fn totals(requests: u64, input: u64) -> UsageTotals {
    UsageTotals { requests, input_tokens: input, output_tokens: 1, cache_read_tokens: 2, cache_write_tokens: 3 }
}

fn entry(agent: &str, session: Option<&str>, model: &str, t: UsageTotals) -> UsageEntry {
    UsageEntry {
        agent_id: AgentId::new(agent),
        session_id: session.map(SessionId::new),
        kind: RunKind::Run,
        model: model.into(),
        totals: t,
    }
}

async fn log() -> (tempfile::TempDir, UsageLog) {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("t.db");
    muliao_platform::db::migrate(&path, &[MIGRATION]).unwrap();
    let db = Arc::new(Database::open(&path, Arc::new(|| {})).unwrap());
    (dir, UsageLog::new(db, chrono_tz::Asia::Shanghai))
}

/// 直接写一行，指定时间（record 用的是当前时间）
async fn insert_at(log: &UsageLog, at: DateTime<Utc>, e: UsageEntry) {
    log.db
        .write(move |tx| {
            let t = e.totals;
            tx.execute(
                "INSERT INTO model_usage (created_at, agent_id, session_id, kind, model, requests, input_tokens,
                    output_tokens, cache_read_tokens, cache_write_tokens) VALUES (?1,?2,?3,?4,?5,?6,?7,?8,?9,?10)",
                rusqlite::params![
                    at,
                    e.agent_id,
                    e.session_id,
                    e.kind.as_str(),
                    e.model,
                    t.requests,
                    t.input_tokens,
                    t.output_tokens,
                    t.cache_read_tokens,
                    t.cache_write_tokens
                ],
            )
        })
        .await
        .unwrap();
}

#[tokio::test(flavor = "multi_thread")]
async fn record_and_session_totals() {
    let (_dir, log) = log().await;
    assert_eq!(log.session_totals(&SessionId::new("s1")).await.unwrap(), UsageTotals::default());
    log.record(entry("a", Some("s1"), "p:m", totals(1, 10))).await.unwrap();
    log.record(UsageEntry { kind: RunKind::Compaction, ..entry("a", Some("s1"), "p:m", totals(1, 5)) }).await.unwrap();
    log.record(entry("a", Some("s2"), "p:m", totals(1, 100))).await.unwrap();
    log.record(entry("a", None, "p:m", totals(1, 1000))).await.unwrap();
    let s1 = log.session_totals(&SessionId::new("s1")).await.unwrap();
    assert_eq!(s1, totals(1, 10) + totals(1, 5));
    assert_eq!(s1.input_tokens, 15);
    assert_eq!(s1.cache_write_tokens, 6);
}

#[tokio::test(flavor = "multi_thread")]
async fn summarize_groups_by_day_in_the_configured_timezone() {
    let (_dir, log) = log().await;
    let utc = |d, h| Utc.with_ymd_and_hms(2026, 11, d, h, 0, 0).unwrap();
    // UTC 11-11 15:00 是上海 11-11 23:00；UTC 11-11 17:00 是上海 11-12 01:00：同一个 UTC 日，分属上海两天
    insert_at(&log, utc(11, 15), entry("a", Some("s"), "p:m", totals(1, 10))).await;
    insert_at(&log, utc(11, 17), entry("a", Some("s"), "p:m", totals(1, 20))).await;
    insert_at(&log, utc(11, 18), entry("b", Some("s"), "p:x", totals(2, 40))).await;
    // 范围外（until 不含）
    insert_at(&log, utc(12, 0), entry("a", Some("s"), "p:m", totals(9, 999))).await;

    let since = utc(11, 0);
    let until = utc(12, 0);
    let by_day = log.summarize(since, until, &[UsageGroup::Day]).await.unwrap();
    let day = |d| NaiveDate::from_ymd_opt(2026, 11, d);
    assert_eq!(
        by_day,
        vec![
            UsageBucket { agent_id: None, model: None, day: day(11), totals: totals(1, 10) },
            UsageBucket { agent_id: None, model: None, day: day(12), totals: totals(1, 20) + totals(2, 40) },
        ]
    );

    let by_agent_model = log.summarize(since, until, &[UsageGroup::Model, UsageGroup::Agent]).await.unwrap();
    assert_eq!(
        by_agent_model,
        vec![
            UsageBucket {
                agent_id: Some(AgentId::new("a")),
                model: Some("p:m".into()),
                day: None,
                totals: totals(1, 10) + totals(1, 20)
            },
            UsageBucket {
                agent_id: Some(AgentId::new("b")),
                model: Some("p:x".into()),
                day: None,
                totals: totals(2, 40)
            },
        ]
    );

    let total = log.summarize(since, until, &[]).await.unwrap();
    assert_eq!(total.len(), 1);
    assert_eq!(total[0].totals.requests, 4);
    assert_eq!(total[0].totals.input_tokens, 70);
    assert!(log.summarize(until, utc(12, 0), &[]).await.unwrap().is_empty());
}

#[tokio::test(flavor = "multi_thread")]
async fn time_ranges_compare_correctly_with_fractional_seconds() {
    // 存储的文本形式小数位不定长（整秒时没有小数部分）：范围比较仍按时间先后
    let (_dir, log) = log().await;
    let whole = Utc.with_ymd_and_hms(2026, 11, 11, 10, 0, 0).unwrap();
    let frac = whole + chrono::Duration::milliseconds(500);
    insert_at(&log, whole, entry("a", None, "p:m", totals(1, 1))).await;
    insert_at(&log, frac, entry("a", None, "p:m", totals(1, 2))).await;
    let only_frac = log
        .summarize(whole + chrono::Duration::milliseconds(1), frac + chrono::Duration::seconds(1), &[])
        .await
        .unwrap();
    assert_eq!(only_frac[0].totals.input_tokens, 2);
    let only_whole = log.summarize(whole, frac, &[]).await.unwrap();
    assert_eq!(only_whole[0].totals.input_tokens, 1);
}

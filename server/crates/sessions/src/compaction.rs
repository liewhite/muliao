//! 会话上下文压缩，与模型提供方无关。
//!
//! - 触发：发给模型的上下文（上一次模型请求的输入 token，含缓存部分）超过阈值时，由会话 actor 在步与步之间压缩：运行
//!   开始时，以及每次请求模型之前（这次请求照常保存为一步，然后停下、压缩、从库里重新加载上下文续跑）。也可由用户在会话
//!   页手动触发（同一个方法）。该不该压缩只看 [`verdict`]。
//! - 执行：把最近 keep_steps 步之前的消息连同上一份摘要交给该 agent 的模型总结，写入 compactions 表。切点在步与步之间：
//!   之前的消息里没有悬空调用的地方，工具调用与其结果、挂起的调用与之后的回答不会被拆开；一次运行中途也有切点（每一批
//!   工具结果之后），长任务在运行中也能压缩。单次只取总长不超过上限的最早若干步，剩下的接着压缩，直到保留窗口之外没有
//!   可压缩的。
//! - 使用：发给模型的上下文 = 最新摘要（放进 instructions）+ 切点之后的原始消息（[`crate::history::load_context`]）。
//!   原始消息不删除。

use std::collections::HashSet;
use std::sync::Arc;

use agenter::Message;
use muliao_assets::tree::AgentDir;
use muliao_platform::db::Database;
use muliao_runtime::Runtime;

use crate::conventions::{calls_in, measured_responses, results_in};
use crate::error::Result;
use crate::history::{Stored, add_compaction_in, latest_compaction, rows_after};
use crate::transcript::{Renders, to_items, to_text};

/// 文字记录里每条的字符上限。
const ITEM_CHARS: usize = 4000;
/// 一次交给模型总结的文字记录的字符上限（超出的步留给下一次）。
const TRANSCRIPT_CHARS: usize = 400_000;

pub const COMPACTION_INSTRUCTIONS: &str = "\
You compress the earlier part of a conversation between a user and an AI agent so the agent can continue \
without the original messages. Write a dense summary in the conversation's language covering: the user's goals \
and requests, decisions made, facts learned, results of tool calls that still matter (file paths, IDs, URLs, \
commands, numbers), work completed, and open tasks or questions. Omit pleasantries and anything superseded. \
Output only the summary.";

/// 上一次模型请求的输入 token 数，即发给模型的上下文的实际大小；还没有报告了用量的回复时为 None。
///
/// stale_responses（[`crate::history::SessionContext::stale_responses`]）：最近一次压缩写入之前就有的回复条数（没压缩过为
/// None），它们报告的是压缩前的大小，不算；压缩之后还没有新回复时结果也是 None。压缩判断与会话页显示的上下文大小都按这一条。
pub fn context_tokens(history: &[Message], stale_responses: Option<usize>) -> Option<u64> {
    let measured = measured_responses(history);
    (measured.len() > stale_responses.unwrap_or(0)).then(|| measured.last().expect("non-empty").usage.input_tokens)
}

/// 按步分组，保持时间顺序。之前的消息里没有悬空调用的地方开始新的一步：工具调用与其结果、挂起的调用与之后的回答总在
/// 同一组里，切点落在组与组之间，留下的结果一定找得到它的调用。留下的部分可能从一条模型回复开始，发给模型时补一条接续的
/// 用户消息（[`crate::history::context_of`]）。
pub fn steps_of(rows: &[Stored]) -> Vec<Vec<&Stored>> {
    let mut steps: Vec<Vec<&Stored>> = Vec::new();
    // 到目前为止还没有结果的调用 id（与 conventions::unanswered_calls 同一个定义，逐条维护）
    let mut open: HashSet<&str> = HashSet::new();
    for row in rows {
        match steps.last_mut() {
            Some(step) if !open.is_empty() => step.push(row),
            _ => steps.push(vec![row]),
        }
        open.extend(calls_in(&row.message).into_iter().map(|c| c.id.as_str()));
        for id in results_in(&row.message) {
            open.remove(id);
        }
    }
    steps
}

fn step_text(step: &[&Stored]) -> Result<String> {
    let items = to_items(step.iter().map(|s| &s.message), &Renders::new(), None)?;
    Ok(to_text(&items, ITEM_CHARS))
}

/// 从最早的可压缩步（最近 keep_steps 步之前的）开始，取文字记录总长不超过 max_chars 的若干步（至少一步）。不截断内容：
/// 超出的下一次再压缩。返回 (这次压缩的步, 是否还有可压缩的留给下一次)。
pub fn select_steps<'a, 'r>(
    steps: &'a [Vec<&'r Stored>],
    keep_steps: usize,
    max_chars: usize,
) -> Result<(&'a [Vec<&'r Stored>], bool)> {
    // keep_steps 为 0 时全部可压缩（Python 的 steps[:-0] 是空列表，随后出错；配置下限是 1，碰不到）
    let compactable = &steps[..steps.len().saturating_sub(keep_steps)];
    let mut chosen = 0;
    let mut total = 0;
    for step in compactable {
        let size = step_text(step)?.chars().count();
        if chosen > 0 && total + size > max_chars {
            break;
        }
        chosen += 1;
        total += size;
    }
    Ok((&compactable[..chosen], chosen < compactable.len()))
}

/// 交给模型总结的内容：上一份摘要（有的话）与这些步的文字记录。
pub fn compaction_prompt(previous_summary: Option<&str>, steps: &[Vec<&Stored>]) -> Result<String> {
    let transcript = steps.iter().map(|s| step_text(s)).collect::<Result<Vec<_>>>()?.join("\n");
    let previous = previous_summary
        .filter(|s| !s.is_empty())
        .map(|s| format!("<previous_summary>\n{s}\n</previous_summary>\n\n"))
        .unwrap_or_default();
    Ok(format!("{previous}<conversation>\n{transcript}\n</conversation>\n\nWrite the updated summary."))
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Verdict {
    NotDue,
    Due,
    /// 刚压缩过，上下文仍超阈值：保留窗口本身就超了
    Hopeless,
}

/// 该不该压缩，唯一规则。只认最近一次压缩之后的回复报告的 token 数（[`context_tokens`]）。压缩后的第一次回复就超阈值，
/// 说明保留窗口本身就超了，再压也压不下去（Hopeless）：这次运行不再压缩，上下文继续变大直到超出模型的上限时运行失败
/// 可见，重试时重新判断（用最终正确性换掉「保留窗口也要切」的复杂度）。
pub fn verdict(threshold: u64, session_id: &str, history: &[Message], stale_responses: Option<usize>) -> Verdict {
    let Some(tokens) = context_tokens(history, stale_responses) else { return Verdict::NotDue };
    if tokens == 0 {
        // 部分 OpenAI 兼容服务流式时不返回 usage：无法判断上下文大小，自动压缩对该模型失效
        tracing::warn!(session_id, "model reported no input token usage, automatic compaction cannot trigger");
        return Verdict::NotDue;
    }
    if tokens <= threshold {
        return Verdict::NotDue;
    }
    if let Some(stale) = stale_responses
        && measured_responses(history).len() == stale + 1
    {
        tracing::warn!(
            session_id,
            tokens,
            threshold,
            "context still over the compaction threshold right after compacting; not compacting again this run"
        );
        return Verdict::Hopeless;
    }
    Verdict::Due
}

pub struct Compactor {
    pub db: Arc<Database>,
    pub runtime: Arc<Runtime>,
    /// 压缩阈值（config `[runtime] compaction_threshold_tokens`），所有会话同一个；判断用 [`verdict`]
    pub threshold_tokens: u64,
    /// 保留最近这么多步原文不压缩（config `[runtime] compaction_keep_steps`）
    pub keep_steps: usize,
}

impl Compactor {
    /// 用这份 agent 配置（运行中压缩时是运行的快照）的模型压缩到保留窗口之外没有可压缩的为止（每一批一次模型调用）。
    /// 返回压缩的步数；为 0 表示没有可压缩的。模型调用失败时之前批次写下的压缩仍在（每批各自提交）。
    pub async fn compact(&self, session_id: &str, config: &AgentDir) -> Result<usize> {
        let mut compacted = 0;
        loop {
            let sid = session_id.to_owned();
            let (previous, rows) = self
                .db
                .read(move |c| -> Result<_> {
                    let previous = latest_compaction(c, &sid)?;
                    let rows = rows_after(c, &sid, previous.as_ref().map_or(0, |p| p.through_message_id))?;
                    Ok((previous, rows))
                })
                .await?;
            let steps = steps_of(&rows);
            if steps.len() <= self.keep_steps {
                break;
            }
            let (chosen, more) = select_steps(&steps, self.keep_steps, TRANSCRIPT_CHARS)?;
            let prompt = compaction_prompt(previous.as_ref().map(|p| p.summary.as_str()), chosen)?;
            let summary = self.runtime.summarize(config, session_id, COMPACTION_INSTRUCTIONS, &prompt).await?;
            let through = chosen.last().and_then(|s| s.last()).expect("at least one step is chosen").id;
            let sid = session_id.to_owned();
            self.db.write(move |tx| add_compaction_in(tx, &sid, through, &summary)).await?;
            compacted += chosen.len();
            tracing::info!(session_id, steps = chosen.len(), through_message_id = through, more, "session compacted");
            if !more {
                break;
            }
        }
        Ok(compacted)
    }
}

#[cfg(test)]
mod tests {
    use agenter::{Content, Request, RequestPart, Response, ResponsePart, ToolCall, ToolResult, Usage};
    use chrono::Utc;

    use super::*;

    fn stored(id: i64, message: Message) -> Stored {
        Stored { id, message, created_at: Utc::now(), run_id: 1 }
    }

    fn user(text: &str) -> Message {
        Message::User(Request {
            parts: vec![RequestPart::Text { text: text.into() }],
            timestamp: Utc::now(),
            metadata: None,
        })
    }

    fn response(parts: Vec<ResponsePart>, input_tokens: u64) -> Message {
        Message::Assistant(Response {
            parts,
            protocol: "anthropic".into(),
            model: "m".into(),
            response_id: None,
            finish_reason: None,
            usage: Usage { input_tokens, ..Default::default() },
            timestamp: Utc::now(),
            metadata: None,
        })
    }

    fn calls(ids: &[&str]) -> Message {
        response(
            ids.iter()
                .map(|i| {
                    ResponsePart::ToolCall(ToolCall {
                        id: (*i).into(),
                        name: "t".into(),
                        args: "{}".into(),
                        meta: None,
                    })
                })
                .collect(),
            1,
        )
    }

    fn results(ids: &[&str]) -> Message {
        Message::User(Request {
            parts: ids
                .iter()
                .map(|i| {
                    RequestPart::ToolResult(ToolResult {
                        call_id: (*i).into(),
                        tool_name: "t".into(),
                        content: vec![Content::text("ok")],
                        is_error: false,
                        metadata: None,
                    })
                })
                .collect(),
            timestamp: Utc::now(),
            metadata: None,
        })
    }

    fn ids(steps: &[Vec<&Stored>]) -> Vec<Vec<i64>> {
        steps.iter().map(|s| s.iter().map(|r| r.id).collect()).collect()
    }

    #[test]
    fn steps_keep_calls_with_their_results() {
        let rows = vec![
            stored(1, user("go")),
            stored(2, calls(&["a", "b"])),
            stored(3, results(&["a"])),
            stored(4, results(&["b"])),
            // 挂起的提问：调用之后先来了别的消息，回答在后面
            stored(5, calls(&["ask"])),
            stored(6, user("new message")),
            stored(7, results(&["ask"])),
            stored(8, response(vec![ResponsePart::Text { text: "done".into(), meta: None }], 1)),
        ];
        assert_eq!(ids(&steps_of(&rows)), [vec![1], vec![2, 3, 4], vec![5, 6, 7], vec![8]]);
    }

    #[test]
    fn select_steps_bounds_the_transcript_but_takes_at_least_one() {
        let rows: Vec<Stored> = (1..=5).map(|i| stored(i, user(&"x".repeat(100)))).collect();
        let steps = steps_of(&rows);
        let size = step_text(&steps[0]).unwrap().chars().count();

        let (chosen, more) = select_steps(&steps, 2, size * 2).unwrap();
        assert_eq!((ids(chosen), more), (vec![vec![1], vec![2]], true));

        let (chosen, more) = select_steps(&steps, 2, 1).unwrap();
        assert_eq!((ids(chosen), more), (vec![vec![1]], true));

        let (chosen, more) = select_steps(&steps, 2, usize::MAX).unwrap();
        assert_eq!((ids(chosen), more), (vec![vec![1], vec![2], vec![3]], false));

        let prompt = compaction_prompt(Some("before"), chosen).unwrap();
        assert!(prompt.starts_with("<previous_summary>\nbefore\n</previous_summary>\n\n<conversation>\n[USER] x"));
        assert!(prompt.ends_with("</conversation>\n\nWrite the updated summary."));
    }

    #[test]
    fn verdicts() {
        let say = |tokens| response(vec![ResponsePart::Text { text: "hi".into(), meta: None }], tokens);
        let h = [user("a"), say(500)];
        assert_eq!(context_tokens(&h, None), Some(500));
        assert_eq!(verdict(1000, "s", &h, None), Verdict::NotDue);
        assert_eq!(verdict(100, "s", &h, None), Verdict::Due);
        assert_eq!(verdict(100, "s", &[user("a"), say(0)], None), Verdict::NotDue);
        // 压缩之后还没有新回复：不判断
        assert_eq!(context_tokens(&h, Some(1)), None);
        assert_eq!(verdict(100, "s", &h, Some(1)), Verdict::NotDue);
        // 压缩之后的第一条回复仍超阈值
        let h = [user("a"), say(500), say(500)];
        assert_eq!(verdict(100, "s", &h, Some(1)), Verdict::Hopeless);
        let h = [user("a"), say(500), say(500), say(500)];
        assert_eq!(verdict(100, "s", &h, Some(1)), Verdict::Due);
    }
}

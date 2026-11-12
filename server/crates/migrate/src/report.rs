//! 迁移报告：每张表迁了多少、丢了什么、为什么；消息转换的说明；迁完之后的检查结果。写成 Markdown。

use std::fmt::Write;

use crate::messages::Notes;

/// 一张新表的来历。
#[derive(Debug, Default)]
pub struct TableReport {
    pub table: String,
    /// 来源（旧表，或「旧表的哪部分」）
    pub source: String,
    pub old_rows: i64,
    pub new_rows: i64,
    /// 没迁的行：原因 → 条数（加起来等于 old_rows - new_rows，迁移时核对）
    pub skipped: Vec<(String, i64)>,
    /// 迁了但改了内容的：说明 → 条数
    pub changed: Vec<(String, i64)>,
}

#[derive(Debug, Default)]
pub struct Report {
    pub old_db: String,
    pub new_db: String,
    pub tables: Vec<TableReport>,
    /// 旧库里整张不迁的表与原因
    pub dropped_tables: Vec<(String, i64, String)>,
    pub message_notes: Notes,
    /// 迁完后的检查（verify）：做了什么 → 结果；问题单列
    pub checks: Vec<String>,
    pub problems: Vec<String>,
}

impl Report {
    pub fn to_markdown(&self) -> String {
        let mut out = String::new();
        let w = &mut out;
        let _ = writeln!(w, "# muliao 旧库迁移报告\n");
        let _ = writeln!(w, "- 旧库（只读）：`{}`\n- 新库：`{}`\n", self.old_db, self.new_db);
        let _ = writeln!(w, "## 各表\n");
        let _ = writeln!(w, "| 新表 | 来源 | 旧库行数 | 迁入 | 没迁的（原因：条数） | 迁时改了的（说明：条数） |");
        let _ = writeln!(w, "|---|---|---:|---:|---|---|");
        for t in &self.tables {
            let list = |v: &[(String, i64)]| {
                let items: Vec<String> = v.iter().filter(|(_, n)| *n > 0).map(|(r, n)| format!("{r}：{n}")).collect();
                if items.is_empty() { "—".to_owned() } else { items.join("；") }
            };
            let _ = writeln!(
                w,
                "| {} | {} | {} | {} | {} | {} |",
                t.table,
                t.source,
                t.old_rows,
                t.new_rows,
                list(&t.skipped),
                list(&t.changed)
            );
        }
        if !self.dropped_tables.is_empty() {
            let _ = writeln!(w, "\n## 整张不迁的旧表\n");
            for (table, rows, why) in &self.dropped_tables {
                let _ = writeln!(w, "- `{table}`（{rows} 行）：{why}");
            }
        }
        let _ = writeln!(w, "\n## 消息转换说明（pydantic-ai → agenter）\n");
        for (what, n) in &self.message_notes {
            let _ = writeln!(w, "- {what}：{n}");
        }
        if !self.checks.is_empty() || !self.problems.is_empty() {
            let _ = writeln!(w, "\n## 迁完后的检查\n");
            for c in &self.checks {
                let _ = writeln!(w, "- {c}");
            }
            if self.problems.is_empty() {
                let _ = writeln!(w, "\n没有发现问题。");
            } else {
                let _ = writeln!(w, "\n### 发现的问题（{} 个）\n", self.problems.len());
                for p in &self.problems {
                    let _ = writeln!(w, "- {p}");
                }
            }
        }
        out
    }
}

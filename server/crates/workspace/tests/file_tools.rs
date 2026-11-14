//! 文件工具（照 Python 版 tests/test_tools.py 的同名用例，输出逐字比对）。

mod common;

use agenter::{Content, FileSource};
use common::{Out, world};
use serde_json::json;

#[tokio::test]
async fn read_file_offsets_are_1_based_line_numbers() {
    let w = world(20_000).await;
    let out = w
        .run(vec![
            ("write_file", json!({"path": "x.txt", "content": "a\nb\nc\n"})),
            ("read_file", json!({"path": "x.txt", "offset": 2})),
            ("read_file", json!({"path": "x.txt", "offset": 0})),
            ("read_file", json!({"path": "x.txt", "offset": -10})),
        ])
        .await;
    assert_eq!(out[1], "     2\tb\n     3\tc");
    assert!(out[2..].iter().all(|o| o.contains("offset is a 1-based line number (offset=1 is the first line)")));
}

#[tokio::test]
async fn edit_file_replace_all_and_helpful_misses() {
    let w = world(20_000).await;
    let content = "def f():\n    x = 1\n    return x\n\nx = 2\n";
    let out = w
        .run(vec![
            ("write_file", json!({"path": "a.py", "content": content})),
            ("edit_file", json!({"path": "a.py", "old_text": "x", "new_text": "y"})),
            ("edit_file", json!({"path": "a.py", "old_text": "x = 1\nreturn x", "new_text": "z"})),
            ("edit_file", json!({"path": "a.py", "old_text": "    x = 1\n    return 0", "new_text": "z"})),
            ("edit_file", json!({"path": "a.py", "old_text": "x", "new_text": "y", "replace_all": true})),
            ("read_file", json!({"path": "a.py"})),
        ])
        .await;
    assert!(out[1].contains("at lines 2, 3, 5"));
    assert!(out[2].contains("lines 2-3 match it except for whitespace"));
    assert!(out[3].contains("first non-blank line is at line 2"));
    assert!(out[4].contains("3 replacements"));
    assert!(out[5].contains("y = 1") && out[5].contains("return y") && out[5].contains("y = 2"));
}

#[tokio::test]
async fn edit_file_applies_several_edits_all_or_nothing() {
    let w = world(20_000).await;
    let out = w
        .run(vec![
            ("write_file", json!({"path": "m.py", "content": "a = 1\nb = 2\nc = 3\n"})),
            (
                "edit_file",
                json!({"path": "m.py", "edits": [
                    {"old_text": "a = 1", "new_text": "a = 10"},
                    {"old_text": "missing", "new_text": "x"},
                ]}),
            ),
            ("read_file", json!({"path": "m.py"})),
            (
                "edit_file",
                json!({"path": "m.py", "edits": [
                    {"old_text": "a = 1", "new_text": "a = 10"},
                    {"old_text": "a = 10\nb", "new_text": "a = 10\nbb"},
                    {"old_text": " = ", "new_text": "=", "replace_all": true},
                ]}),
            ),
            ("read_file", json!({"path": "m.py"})),
            ("edit_file", json!({"path": "m.py", "old_text": "a", "new_text": "z", "edits": [{"old_text": "b", "new_text": "y"}]})),
            ("edit_file", json!({"path": "m.py", "new_text": "z"})),
            ("edit_file", json!({"path": "m.py", "old_text": "a=10"})),
            ("edit_file", json!({"path": "m.py", "new_text": "", "edits": [{"old_text": "b", "new_text": "y"}]})),
            ("read_file", json!({"path": "m.py"})),
        ])
        .await;
    assert!(out[1].contains("edit 2 of 2") && out[1].contains("1 earlier edit(s) to it applied"));
    assert!(out[1].contains("nothing was written"));
    assert!(out[2].contains("     1\ta = 1\n     2\tb = 2\n     3\tc = 3"));
    assert!(out[3].contains("5 replacements"));
    assert!(out[4].contains("     1\ta=10\n     2\tbb=2\n     3\tc=3"));
    assert!(out[5].contains("not both"));
    assert!(out[6].contains("locate the text in exactly one way"));
    assert!(out[7].contains("new_text is required"));
    assert!(out[8].contains("not both"));
    assert!(out[9].contains("     1\ta=10\n     2\tbb=2\n     3\tc=3"));
}

#[tokio::test]
async fn edit_file_changes_several_files_in_one_call_all_or_nothing() {
    let w = world(20_000).await;
    let out = w
        .run(vec![
            ("write_file", json!({"path": "a.py", "content": "A = 1\n"})),
            ("write_file", json!({"path": "sub/b.py", "content": "B = 1\n"})),
            (
                "edit_file",
                json!({"edits": [
                    {"path": "a.py", "old_text": "A = 1", "new_text": "A = 2"},
                    {"path": "sub/b.py", "old_text": "missing", "new_text": "x"},
                ]}),
            ),
            ("read_file", json!({"reads": [{"path": "a.py"}, {"path": "sub/b.py"}]})),
            (
                "edit_file",
                json!({"path": "a.py", "edits": [
                    {"old_text": "A = 1", "new_text": "A = 2"},
                    {"path": "sub/b.py", "old_text": "B = 1", "new_text": "B = 2"},
                    {"path": "sub/../a.py", "old_text": "A = 2", "new_text": "A = 3"},
                ]}),
            ),
            ("read_file", json!({"reads": [{"path": "a.py"}, {"path": "sub/b.py"}]})),
            ("edit_file", json!({"edits": [{"old_text": "A", "new_text": "Z"}]})),
            ("edit_file", json!({"edits": [{"path": "nope.py", "old_text": "A", "new_text": "Z"}]})),
            ("edit_file", json!({"old_text": "A", "new_text": "Z"})),
        ])
        .await;
    assert!(out[2].contains("edit 2 of 2") && out[2].contains("b.py") && out[2].contains("nothing was written"));
    assert!(out[3].contains("     1\tA = 1") && out[3].contains("     1\tB = 1"));
    let report = &out[4];
    assert!(
        report.contains("a.py (2 replacements; now 1 lines)")
            && report.contains("sub/b.py (1 replacement; now 1 lines)")
    );
    // 回显的次序同 Python：按第一次改到它的次序
    assert!(report.find("a.py (2").unwrap() < report.find("sub/b.py").unwrap());
    assert!(!report.contains("sub/../a.py"));
    assert!(out[5].contains("     1\tA = 3") && out[5].contains("     1\tB = 2"));
    assert!(out[6].contains("edit 1 has no path"));
    assert!(out[7].contains("file not found") && out[7].contains("nope.py"));
    assert!(out[8].contains("path is required"));
}

#[tokio::test]
async fn edit_file_refuses_a_read_only_file_before_writing_any() {
    let w = world(20_000).await;
    w.run(vec![
        ("write_file", json!({"path": "rw.py", "content": "A = 1\n"})),
        ("write_file", json!({"path": "ro.py", "content": "B = 1\n"})),
    ])
    .await;
    use std::os::unix::fs::PermissionsExt;
    std::fs::set_permissions(w.ws().join("ro.py"), std::fs::Permissions::from_mode(0o444)).unwrap();
    let out = w
        .run(vec![
            (
                "edit_file",
                json!({"edits": [
                    {"path": "rw.py", "old_text": "A = 1", "new_text": "A = 2"},
                    {"path": "ro.py", "old_text": "B = 1", "new_text": "B = 2"},
                ]}),
            ),
            ("read_file", json!({"path": "rw.py"})),
        ])
        .await;
    assert!(out[0].contains("edit 2: file is not writable") && out[0].contains("nothing was written"), "{}", out[0]);
    assert!(!out[0].contains("chmod"));
    assert!(out[1].contains("     1\tA = 1"));
}

#[tokio::test]
async fn edit_file_locates_by_line_range_and_validates_the_locator() {
    let w = world(20_000).await;
    let out = w
        .run(vec![
            ("write_file", json!({"path": "m.py", "content": "import os\n\ndef keep():\n    return 1\n"})),
            ("edit_file", json!({"path": "m.py", "start_line": 3, "end_line": 4, "anchor": "def keep():", "new_text": "K = 1"})),
            ("edit_file", json!({"path": "m.py", "start_line": 3, "end_line": 3, "anchor": "def keep():", "new_text": ""})),
            (
                "edit_file",
                json!({"path": "m.py", "edits": [
                    {"start_line": 3, "end_line": 3, "anchor": "K = 1", "new_text": "K = 2"},
                    {"old_text": "import os", "new_text": "import os, sys"},
                ]}),
            ),
            ("edit_file", json!({"path": "m.py", "edits": [{"old_text": "K", "start_line": 1, "end_line": 1, "anchor": "x", "new_text": "x"}]})),
            ("edit_file", json!({"path": "m.py", "start_line": 1, "new_text": "x"})),
            ("edit_file", json!({"path": "m.py", "start_line": 1, "end_line": 1, "anchor": "import os, sys", "new_text": "x", "replace_all": true})),
            ("edit_file", json!({"path": "m.py", "edits": [{"old_text": "K = 2", "new_text": "K = 3"}, {"old_text": "nothing", "new_text": ""}]})),
            ("edit_file", json!({"path": "m.py", "new_text": "x"})),
            ("read_file", json!({"path": "m.py"})),
        ])
        .await;
    assert!(out[1].contains("(1 replacement; now 3 lines)") && out[1].contains("     3\tK = 1"));
    assert!(out[2].contains("anchor does not match line 3") && out[2].contains("     3\tK = 1"));
    assert!(out[3].contains("(2 replacements; now 3 lines)"));
    assert!(out[4].contains("exactly one way"));
    assert!(out[5].contains("go together"));
    assert!(out[6].contains("replace_all does not apply to a line range"));
    assert!(out[7].contains("edit 2 of 2") && out[7].contains("must occur exactly once"));
    assert!(out[7].contains("nothing was written"));
    assert!(out[8].contains("exactly one way"));
    assert_eq!(out[9], "     1\timport os, sys\n     2\t\n     3\tK = 2");
}

#[tokio::test]
async fn read_file_reads_several_parts_and_reports_bad_parts_inline() {
    let w = world(20_000).await;
    let content: String = (1..=10).map(|i| format!("line{i}\n")).collect();
    let out = w
        .run(vec![
            ("write_file", json!({"path": "a.txt", "content": content})),
            ("write_file", json!({"path": "b.txt", "content": "only\n"})),
            ("write_file", json!({"path": "empty.txt", "content": ""})),
            (
                "read_file",
                json!({"reads": [
                    {"path": "a.txt", "offset": 2, "limit": 2},
                    {"path": "missing.txt"},
                    {"path": "a.txt", "offset": 9},
                    {"path": "b.txt"},
                    {"path": "a.txt", "offset": 50},
                    {"path": "empty.txt"},
                ]}),
            ),
            ("read_file", json!({"path": "a.txt", "reads": [{"path": "b.txt"}]})),
            ("read_file", json!({})),
        ])
        .await;
    assert!(out[2].contains("(0 lines, 0 chars)"));
    let o = &out[3];
    assert!(
        o.contains("a.txt lines 2-3 of 10 <==\n     2\tline2\n     3\tline3\n[7 more lines; continue with offset=4]")
    );
    assert!(o.contains("missing.txt: file not found"));
    assert!(o.contains("a.txt lines 9-10 of 10 <==\n     9\tline9\n    10\tline10"));
    assert!(o.contains("b.txt lines 1-1 of 1 <==\n     1\tonly"));
    assert!(o.contains("==> a.txt: offset 50 is past the end"));
    assert!(o.contains("empty.txt (empty file) <=="));
    let at = |s: &str| o.find(s).unwrap();
    assert!(
        at("lines 2-3") < at("missing.txt")
            && at("missing.txt") < at("lines 9-10")
            && at("lines 9-10") < at("b.txt lines")
    );
    assert!(out[4].contains("not both"));
    assert!(out[5].contains("path is required"));
}

#[tokio::test]
async fn edit_file_shows_the_changed_lines_with_numbers() {
    let w = world(20_000).await;
    let content: String = (1..=20).map(|i| format!("l{i}\n")).collect();
    let out = w
        .run(vec![
            ("write_file", json!({"path": "e.txt", "content": content})),
            ("write_file", json!({"path": "e.txt", "content": content})),
            (
                "edit_file",
                json!({"path": "e.txt", "edits": [
                    {"old_text": "l3\n", "new_text": "l3\nnew\n"},
                    {"old_text": "l15\n", "new_text": ""},
                ]}),
            ),
            ("edit_file", json!({"path": "e.txt", "old_text": "l1\n", "new_text": "l1\n"})),
            ("write_file", json!({"path": "n.txt", "content": "a\nb"})),
            ("edit_file", json!({"path": "n.txt", "old_text": "b", "new_text": "b\n"})),
            ("edit_file", json!({"path": "n.txt", "old_text": "a\nb\n", "new_text": ""})),
        ])
        .await;
    assert!(out[0].starts_with("created ") && out[0].contains("(20 lines, "));
    assert!(out[1].starts_with("overwrote "));
    assert!(out[2].starts_with("edited "));
    assert!(out[2].ends_with(
        "(2 replacements; now 20 lines); cat -n of the changed lines with 2 lines of context:\n     2\tl2\n     3\tl3\n     \
         4\tnew\n     5\tl4\n     6\tl5\n--\n    14\tl13\n    15\tl14\n    16\tl16\n    17\tl17"
    ));
    assert!(out[3].contains("the text is unchanged"));
    assert!(out[5].contains("only line endings changed"));
    assert!(out[6].contains("now 0 lines); the file is now empty"));
}

#[tokio::test]
async fn crlf_files_are_edited_as_lf_and_written_back_as_crlf() {
    let w = world(20_000).await;
    std::fs::write(w.ws().join("w.txt"), "a\r\nb\r\n").unwrap();
    let out = w.run(vec![("edit_file", json!({"path": "w.txt", "old_text": "a\nb", "new_text": "a\nB"}))]).await;
    assert!(out[0].contains("(1 replacement; now 2 lines)"), "{}", out[0]);
    assert_eq!(std::fs::read_to_string(w.ws().join("w.txt")).unwrap(), "a\r\nB\r\n");
}

const N_SKIPPED: usize = 30;

#[tokio::test]
async fn read_file_stops_at_a_line_boundary_at_the_output_limit_and_says_where_to_continue() {
    let w = world(1000).await;
    let content: String = (1..=300).map(|i| format!("line{i:03}\n")).collect();
    let mut many = vec![
        json!({"path": "a.txt", "limit": 3}),
        json!({"path": "a.txt", "offset": 11, "limit": 3}),
        json!({"path": "wide.txt"}),
    ];
    many.extend((1..=N_SKIPPED).map(|i| json!({"path": format!("d{i:02}.py"), "offset": i})));
    let out = w
        .run(vec![
            ("write_file", json!({"path": "a.txt", "content": content})),
            ("read_file", json!({"path": "a.txt"})),
            ("read_file", json!({"path": "a.txt", "offset": 11, "limit": 200})),
            ("read_file", json!({"reads": [{"path": "a.txt", "limit": 5}, {"path": "a.txt"}, {"path": "b.txt"}]})),
            ("write_file", json!({"path": "wide.txt", "content": format!("{}\n", "w".repeat(1500))})),
            ("read_file", json!({"reads": many})),
        ])
        .await;
    let single = &out[1];
    assert!(single.chars().count() <= 1000 && !single.contains("truncated"));
    let (body, note) = single.rsplit_once('\n').unwrap();
    let last: usize = body.rsplit_once('\n').unwrap().1.split('\t').next().unwrap().trim().parse().unwrap();
    assert!(body.ends_with(&format!("{last:6}\tline{last:03}")));
    assert_eq!(note, format!("[output limit reached at line {last} of 300; continue with offset={}]", last + 1));
    let ranged = &out[2];
    let stop: usize =
        ranged.rsplit_once("limit reached at line ").unwrap().1.split(' ').next().unwrap().parse().unwrap();
    assert!(ranged.starts_with("    11\tline011"));
    assert!(ranged.ends_with(&format!("continue with offset={}, limit={}]", stop + 1, 210 - stop)));
    let multi = &out[3];
    assert!(multi.chars().count() <= 1000 && !multi.contains("truncated"));
    assert!(
        multi.contains("a.txt lines 1-5 of 300 <==") && multi.contains("\n[295 more lines; continue with offset=6]")
    );
    let second = multi.split("\n\n").nth(1).unwrap();
    let stop: usize =
        second.rsplit_once("limit reached at line ").unwrap().1.split(' ').next().unwrap().parse().unwrap();
    assert!(second.contains(&format!("a.txt lines 1-{stop} of 300 <==")));
    assert!(second.ends_with(&format!("continue with offset={}]", stop + 1)));
    assert!(multi.ends_with("\n\n[not read, output limit reached; read in another call: b.txt offset=1]"));
    let many = &out[5];
    assert!(many.chars().count() <= 1000 && !many.contains("truncated"));
    let (head, tail) = many.split_once("\n\n[not read, output limit reached; read in another call: ").unwrap();
    assert!(head.starts_with("==> "));
    assert!(head.ends_with(
        "a.txt lines 1-3 of 300 <==\n     1\tline001\n     2\tline002\n     3\tline003\n[297 more lines; continue with offset=4]"
    ));
    let listed: Vec<&str> = tail.strip_suffix(']').unwrap().split("; ").collect();
    assert_eq!(listed[..2], ["a.txt offset=11 limit=3", "wide.txt offset=1"]);
    let rest: Vec<String> = (1..=N_SKIPPED).map(|i| format!("d{i:02}.py offset={i}")).collect();
    assert_eq!(listed[2..], rest[..]);
}

#[tokio::test]
async fn read_file_truncates_very_long_lines() {
    let w = world(20_000).await;
    let out = w
        .run(vec![
            ("write_file", json!({"path": "min.js", "content": format!("{}\nok\n", "a".repeat(5000))})),
            ("read_file", json!({"path": "min.js"})),
        ])
        .await;
    assert!(out[1].contains("…[line truncated: 3000 more chars]") && out[1].contains("     2\tok"));
    assert!(out[1].chars().count() < 2100);
}

#[tokio::test]
async fn read_file_returns_images_as_content() {
    let w = world(20_000).await;
    std::fs::write(w.ws().join("shot.png"), b"\x89PNG fake").unwrap();
    std::fs::write(w.ws().join("huge.png"), vec![b'x'; (1 << 20) + 1]).unwrap();
    let (out, content) = w.call_in(&w.scope(), "read_file", json!({"path": "shot.png"}), None).await;
    assert!(out.ok().ends_with("shot.png (image/png, 9 bytes)"));
    assert!(
        matches!(&content[1], Content::File(f) if f.media_type == "image/png" && f.source == FileSource::Data(b"\x89PNG fake".to_vec()))
    );
    let out = w
        .run(vec![
            ("read_file", json!({"path": "shot.png", "offset": 3})),
            ("read_file", json!({"reads": [{"path": "shot.png"}]})),
            ("read_file", json!({"path": "gone.webp"})),
            ("read_file", json!({"path": "huge.png"})),
        ])
        .await;
    assert!(out[0].contains("offset/limit do not apply to an image file"));
    assert!(out[1].contains("an image file cannot be read in reads"));
    assert!(out[2].contains("file not found"));
    assert!(out[3].contains("image too large to view") && out[3].contains("make a smaller copy first"));
}

#[tokio::test]
async fn share_file_attaches_the_file_to_the_result() {
    let w = world(20_000).await;
    std::fs::write(w.ws().join("report.pdf"), b"%PDF").unwrap();
    let scope = w.scope();
    let (out, _) = w.call_in(&scope, "share_file", json!({"path": "report.pdf"}), None).await;
    assert_eq!(out.ok(), "shared file report.pdf with the user; it is shown with your reply");
    let meta = scope.take_result_metadata("call-share_file").unwrap();
    let meta: muliao_runtime::conventions::ResultFiles = serde_json::from_value(meta).unwrap();
    let attached = &meta.attachments[0];
    assert_eq!((attached.name.as_str(), attached.media_type.as_str()), ("report.pdf", "application/pdf"));
    assert!(w.call("share_file", json!({"path": "nope.txt"})).await.retry().starts_with("file not found: "));
}

#[tokio::test]
async fn writes_outside_the_writable_paths_are_refused_with_the_reason() {
    let w = world(20_000).await;
    let theirs = w.asset_dir("other").join("memory.md");
    let out = w.call("write_file", json!({"path": theirs, "content": "x"})).await;
    let text = out.retry();
    assert!(text.starts_with(&format!("[Errno 1] Operation not permitted: '{}'", theirs.display())), "{text}");
    assert!(text.contains("\"other\"") || text.contains("other/"), "{text}");
    // 平台的私有文件连读都不行
    std::fs::write(w.dir.path().join("config.toml"), "secret = 1").unwrap();
    let out = w.call("read_file", json!({"path": w.dir.path().join("config.toml")})).await;
    assert!(out.retry().contains("one of the platform's private files"));
}

#[tokio::test]
async fn write_file_replaces_an_asset_file_only_after_reading_it() {
    let w = world(20_000).await;
    let memory = w.asset_dir("me").join("memory.md");
    std::fs::write(&memory, "旧的\n").unwrap();
    let out = w.call("write_file", json!({"path": memory, "content": "新的\n"})).await;
    assert!(out.retry().contains("is an existing AI asset file and write_file replaces it whole"));
    let scope = w.scope();
    w.call_in(&scope, "read_file", json!({"path": memory}), None).await.0.ok();
    let (out, _) = w.call_in(&scope, "write_file", json!({"path": memory, "content": "新的\n"}), None).await;
    assert!(out.ok().starts_with("overwrote "));
    assert_eq!(std::fs::read_to_string(&memory).unwrap(), "新的\n");
    // 读过之后被别处改了
    std::thread::sleep(std::time::Duration::from_millis(20));
    std::fs::write(&memory, "别处改的\n").unwrap();
    let (out, _) = w.call_in(&scope, "write_file", json!({"path": memory, "content": "再改\n"}), None).await;
    assert!(out.retry().contains("was changed elsewhere after you opened or read it"));
    // 格式不对的资源文件被写入口拒绝，说明是谁的、看哪一节
    let bad = w.asset_dir("me").join("skills/x/SKILL.md");
    let out = w.call("write_file", json!({"path": bad, "content": "no frontmatter"})).await;
    assert!(out.retry().contains("nothing was written"), "{:?}", out);
    assert!(matches!(out, Out::Retry(_)));
}

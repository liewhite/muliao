//! 和 Python 版逐字对齐的证据：fixtures 里是 Python 版对同一批输入算出的结果（Python 版与生成脚本 gen_edits.py 已随 #717 删除，要看从 git 历史取）。

use std::collections::{BTreeMap, HashMap};

use muliao_workspace::edits::{Change, apply_each, changed_ranges};
use serde::Deserialize;

#[derive(Deserialize)]
struct EditCase {
    texts: HashMap<String, String>,
    edits: Vec<(String, EditSpec)>,
    error: Option<String>,
    after: Option<BTreeMap<String, String>>,
    counts: Option<BTreeMap<String, usize>>,
    ranges: Option<BTreeMap<String, Vec<(usize, usize)>>>,
}

#[derive(Deserialize)]
#[serde(rename_all = "lowercase")]
enum EditSpec {
    Lines(usize, usize, String, String),
    Replace(String, String, bool),
}

#[test]
fn multi_edits_match_python() {
    let cases: Vec<EditCase> = serde_json::from_str(include_str!("fixtures/edits.json")).unwrap();
    assert!(cases.len() >= 1000);
    for (n, case) in cases.into_iter().enumerate() {
        let edits: Vec<(String, Result<Change, _>)> = case
            .edits
            .into_iter()
            .map(|(k, e)| {
                let change = match e {
                    EditSpec::Lines(start, end, anchor, new) => Change::Lines { start, end, anchor, new },
                    EditSpec::Replace(old, new, every) => Change::Replace { old, new, every },
                };
                (k, Ok(change))
            })
            .collect();
        match apply_each(&case.texts, &edits, |k| k.clone()) {
            Ok((after, counts)) => {
                assert_eq!(case.error, None, "case {n}: Python failed, Rust did not");
                assert_eq!(Some(&after), case.after.as_ref(), "case {n}");
                assert_eq!(Some(&counts), case.counts.as_ref(), "case {n}");
                let ranges: BTreeMap<String, Vec<(usize, usize)>> =
                    after.iter().map(|(k, a)| (k.clone(), changed_ranges(&case.texts[k], a, 2))).collect();
                assert_eq!(Some(&ranges), case.ranges.as_ref(), "case {n}");
            }
            Err(e) => assert_eq!(Some(&e.message), case.error.as_ref(), "case {n}"),
        }
    }
}

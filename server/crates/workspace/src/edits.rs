//! edit_file 的多处修改（纯函数）：每处修改（[`Change`]）按自己的方式定位——原文或行号区间——只报出要做的替换
//! （[`Splice`]），[`apply_each`] 依次拼出新文本；行号一律指调用前的原文，由 [`Origin`] 换算。另有改动处的行范围
//! （[`changed_ranges`]，edit_file 的回显用）。

use std::collections::{BTreeMap, HashMap, HashSet};
use std::hash::Hash;

use muliao_platform::errors::DomainError;

use crate::text::{
    Splice, clip, ends_line, line_starts, locate_once, miss, numbered, occurrences, splice, split_lines,
    split_lines_keep,
};

/// 一处修改。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Change {
    /// 按原文定位：old 恰好出现一次替换为 new；every 时替换每一处（至少一处）。
    Replace { old: String, new: String, every: bool },
    /// 按行号定位：本次调用前（即 read_file 看到的）第 start 到 end 行（1 起算、含两端）整行替换为 new；同一次调用里
    /// 前面的修改挪动了这些行时按 origin 换算到现在的位置，前面的修改碰过这些行就拒绝。anchor 是第 start 行的原文
    /// （两端空白不计），对不上说明行号已过期（读过之后文件又变了），报错并展示那几行的内容，不改错地方。
    Lines { start: usize, end: usize, anchor: String, new: String },
}

impl Change {
    /// 在 current（同一份文本里前面的修改都做完后的样子）里定位，返回要做的各处替换（按位置排列、互不重叠）与替换数。
    /// 只报替换、不自己拼新文本，Origin 才能确知哪些行被改过，不必事后比对猜。
    fn locate(&self, current: &str, what: &str, origin: &Origin) -> Result<(Vec<Splice>, usize), DomainError> {
        match self {
            Change::Replace { old, new, every } => {
                let at = if !every {
                    vec![locate_once(current, old, what)?]
                } else if old.is_empty() {
                    return Err(DomainError::invalid("old text must not be empty"));
                } else {
                    let at = occurrences(current, old);
                    if at.is_empty() {
                        return Err(DomainError::invalid(miss(
                            current,
                            old,
                            0,
                            &format!("old text must occur in {what}"),
                        )));
                    }
                    at
                };
                let count = at.len();
                Ok((at.into_iter().map(|i| Splice { start: i, end: i + old.len(), new: new.clone() }).collect(), count))
            }
            Change::Lines { start, end, anchor, new } => {
                let (start, end) = (*start, *end);
                let original = split_lines_keep(&origin.text);
                if !(1 <= start && start <= end) {
                    return Err(DomainError::invalid(format!(
                        "need 1 <= start_line <= end_line, got start_line={start} end_line={end}"
                    )));
                }
                if end > original.len() {
                    return Err(DomainError::invalid(format!(
                        "end_line {end} is past the end: {what} has {} lines",
                        original.len()
                    )));
                }
                if anchor.trim().contains('\n') {
                    return Err(DomainError::invalid(
                        "anchor must be the text of the single line start_line, not several lines",
                    ));
                }
                let line = original[start - 1].trim_end_matches(['\r', '\n']);
                let anchor = anchor.trim();
                // 超长的行在 read_file 里是截断着展示的，照抄截断后的样子也算对上
                if anchor != line.trim() && anchor != clip(line).trim() {
                    let plain = split_lines(&origin.text);
                    let (first, last) = (start.saturating_sub(3), (end + 2).min(plain.len()));
                    // 只给了行首是最常见的错（#517），单说一句，免得被当成行号过期；但行号真过期时新行也可能碰巧以它开头，
                    // 所以两种都提
                    let prefix = !anchor.is_empty() && line.trim().starts_with(anchor);
                    let advice = if prefix {
                        format!(
                            "anchor must be the whole line as read_file shows it: if line {start} is the one you meant, \
                             give it whole; otherwise the line numbers are stale, give those of the text as it is now"
                        )
                    } else {
                        "give the line numbers and anchor of the text as it is now".to_owned()
                    };
                    return Err(DomainError::invalid(format!(
                        "anchor {} line {start} of {what}; lines {}-{last} are now:\n{}\n{advice}",
                        if prefix { "matches only the beginning of" } else { "does not match" },
                        first + 1,
                        numbered(&plain, first, last),
                    )));
                }
                let (start, end) = origin.now(start, end)?;
                let starts = line_starts(current);
                let (begin, stop) = (starts[start - 1], starts[end]);
                Ok((vec![Splice { start: begin, end: stop, new: as_lines(new, &current[begin..stop]) }], 1))
            }
        }
    }
}

/// 替换整行时的新内容：被替换的行以换行结尾而 new 没有时补上，new 末尾的换行因此可写可不写；new 为空即删掉这些行。
pub fn as_lines(new: &str, replaced: &str) -> String {
    if !new.is_empty() && replaced.ends_with('\n') && !new.ends_with('\n') {
        return format!("{new}\n");
    }
    new.to_owned()
}

/// 一块整行改动里认作没变的行：old 的第 i 行 -> new 的第 j 行。只认首尾相同的行，而且先裁头再裁尾、先裁尾再裁头
/// 两种对法都认同一位置的才算：写出的新行里有和原行相同的（如 `}` 变成两个 `}`）时说不清哪一个是原来的，就算改掉了。
fn unchanged(old: &[&str], new: &[&str]) -> HashMap<usize, usize> {
    let aligned = |head_first: bool| -> HashSet<(usize, usize)> {
        let same = old.len().min(new.len());
        let (mut head, mut tail) = (0, 0);
        let sides: [bool; 2] = if head_first { [true, false] } else { [false, true] };
        for is_head in sides {
            if is_head {
                while head < same - tail && old[head] == new[head] {
                    head += 1;
                }
            } else {
                while tail < same - head && old[old.len() - 1 - tail] == new[new.len() - 1 - tail] {
                    tail += 1;
                }
            }
        }
        let mut pairs: HashSet<(usize, usize)> = (0..head).map(|i| (i, i)).collect();
        pairs.extend((0..tail).map(|k| (old.len() - 1 - k, new.len() - 1 - k)));
        pairs
    };
    aligned(true).intersection(&aligned(false)).copied().collect()
}

/// [`Origin::follow`] 里一块被改动的整行：原来的第 first 到 stop（不含）行；new 是块首到位置 at 这段改后的文本，at
/// 之后到块尾是没动的原文。
struct Block {
    first: usize,
    stop: usize,
    new: String,
    at: usize,
}

impl Block {
    fn text(&self, before: &str, starts: &[usize]) -> String {
        format!("{}{}", self.new, &before[self.at..starts[self.stop]])
    }
}

/// 一份文本在本次调用前的原文，及原文各行在当前文本（做完前面几处修改后）里的位置：批量修改里行号一律指原文（模型
/// 手里的行号来自 read_file，看不到中间状态），由这里换算。每做完一处修改调用 follow 记下它改了哪些行。
pub struct Origin {
    pub text: String,
    /// 当前文本的每一行：(它在原文里是第几行（0 起），不是原文的行为 None；写下它的是第几处修改，原文为 0)
    lines: Vec<(Option<usize>, usize)>,
    /// 被改掉或删掉的原文行 -> 改掉它的是第几处修改
    gone: HashMap<usize, usize>,
}

impl Origin {
    pub fn new(text: String) -> Self {
        let n = split_lines(&text).len();
        Self { text, lines: (0..n).map(|i| (Some(i), 0)).collect(), gone: HashMap::new() }
    }

    /// 第 by 处修改在当前文本 before 上做了 places 这些替换：每处替换扩到所在的整行（替换后不以换行结尾时连上下一行，
    /// 落在同一行的几处合成一块），块外的行原样保留来历；块内只把确定没变的首尾行（[`unchanged`]）认作原来那几行，其余
    /// 原文行记为被第 by 处改掉，新写出的行记为第 by 处写的。只看替换本身、不比对整份文本，重复的行（空行、`}`）不会
    /// 对错位置。
    fn follow(&mut self, before: &str, places: &[Splice], by: usize) {
        let starts = line_starts(before);
        let count = starts.len() - 1;
        // bisect_right(starts, x) - 1：最后一个不大于 x 的行首
        let line_at = |x: usize| starts.partition_point(|&s| s <= x) - 1;
        let mut blocks: Vec<Block> = Vec::new();
        for p in places {
            let first = line_at(p.start);
            let merge = blocks.last().is_some_and(|b| first < b.stop);
            if merge {
                let block = blocks.last_mut().expect("checked");
                block.new.push_str(&before[block.at..p.start]);
                block.new.push_str(&p.new);
            } else {
                blocks.push(Block {
                    first,
                    stop: first,
                    new: format!("{}{}", &before[starts[first]..p.start], p.new),
                    at: p.end,
                });
            }
            let block = blocks.last_mut().expect("just pushed");
            block.at = p.end;
            let touched = if p.end > p.start {
                starts.partition_point(|&s| s < p.end)
            } else if p.start == starts[first] && (p.new.is_empty() || ends_line(&p.new)) {
                // 纯插入：插在行首且自成整行的不碰这一行
                first
            } else {
                first + 1
            };
            block.stop = block.stop.max(touched.min(count));
            // 块的新文本不以换行结尾时，和后面一行连成了一行
            loop {
                let text = block.text(before, &starts);
                if block.stop < count && !ends_line(&text) && !text.is_empty() {
                    block.stop += 1;
                } else {
                    break;
                }
            }
        }
        let mut lines = Vec::new();
        let mut kept = 0;
        for block in &blocks {
            lines.extend_from_slice(&self.lines[kept..block.first]);
            let old_text = &before[starts[block.first]..starts[block.stop]];
            let old = split_lines_keep(old_text);
            let written_text = block.text(before, &starts);
            let written = split_lines_keep(&written_text);
            let stays = unchanged(&old, &written);
            for (i, (original, _)) in self.lines[block.first..block.stop].iter().enumerate() {
                if let Some(original) = original
                    && !stays.contains_key(&i)
                {
                    self.gone.insert(*original, by);
                }
            }
            let moved: HashMap<usize, usize> = stays.iter().map(|(i, j)| (*j, block.first + i)).collect();
            lines.extend((0..written.len()).map(|j| match moved.get(&j) {
                Some(k) => self.lines[*k],
                None => (None, by),
            }));
            kept = block.stop;
        }
        lines.extend_from_slice(&self.lines[kept..]);
        let after = split_lines(&splice(before, places)).len();
        if lines.len() != after {
            // 拼接处生出新的换行（如 "\r" 接上 "\n"）时对不上：不再换算，之后的行号区间一律当作重叠拒绝
            for k in 0..split_lines(&self.text).len() {
                self.gone.insert(k, by);
            }
            lines = vec![(None, by); after];
        }
        self.lines = lines;
    }

    /// 原文第 start 到 end 行（1 起算、含两端）现在是第几到第几行；其中有行被前面的修改改掉、删掉，或中间被插入了新行，
    /// 就是两处修改重叠，拒绝。
    fn now(&self, start: usize, end: usize) -> Result<(usize, usize), DomainError> {
        for k in start - 1..end {
            if let Some(by) = self.gone.get(&k) {
                return Err(overlap(start, end, *by));
            }
        }
        let at: HashMap<usize, usize> =
            self.lines.iter().enumerate().filter_map(|(i, (original, _))| original.map(|o| (o, i))).collect();
        let (first, last) = (at[&(start - 1)], at[&(end - 1)]);
        for i in first..=last {
            if self.lines[i].0.is_none() {
                return Err(overlap(start, end, self.lines[i].1));
            }
        }
        Ok((first + 1, last + 1))
    }
}

fn overlap(start: usize, end: usize, by: usize) -> DomainError {
    let lines = if start == end { format!("line {start}") } else { format!("lines {start}-{end}") };
    DomainError::invalid(format!(
        "{lines} overlap edit {by}, which already changed that part; merge the two into one edit"
    ))
}

/// 多处修改里第 i 处（共 n 处）失败的说明；after 补充这一处是在什么文本上找的。
pub fn failed_edit(i: usize, n: usize, error: &DomainError, after: &str) -> String {
    format!("edit {i} of {n}{after}: {error}; nothing was written")
}

pub const NO_EDITS: &str = "no edits given";

/// [`apply_each`] 的结果：改到的各份的新文本与替换数。
pub type Applied<K> = (BTreeMap<K, String>, BTreeMap<K, usize>);

/// 依次做多处修改，每项作用在它那份文本上（键是文本的名字，如文件路径；what 是报错里怎么称呼它）。同一份文本里按原文
/// 定位的在前面改过的结果上找；按行号定位的，行号一律指调用前的原文，按 [`Origin`] 换算。返回各份的新文本与替换数（只
/// 含被改到的键）。任何一项失败都整体失败，报错指出是第几项；调用方只在成功后写盘，所以不会因为某处对不上而留下改了
/// 一半的文件（写盘本身的 IO 失败不归这里管）。一项是 Err（模型给的定位参数本身不对）时按那一项定位失败报。
pub fn apply_each<K: Eq + Hash + Ord + Clone>(
    texts: &HashMap<K, String>,
    edits: &[(K, Result<Change, DomainError>)],
    what: impl Fn(&K) -> String,
) -> Result<Applied<K>, DomainError> {
    if edits.is_empty() {
        return Err(DomainError::invalid(NO_EDITS));
    }
    let mut current: BTreeMap<K, String> = BTreeMap::new();
    let mut origins: HashMap<K, Origin> = HashMap::new();
    let mut counts: BTreeMap<K, usize> = BTreeMap::new();
    for (i, (key, change)) in edits.iter().enumerate() {
        let i = i + 1;
        let origin = origins.entry(key.clone()).or_insert_with(|| Origin::new(texts[key].clone()));
        let text = current.get(key).cloned().unwrap_or_else(|| origin.text.clone());
        let located = match change {
            Ok(change) => change.locate(&text, &what(key), origin),
            Err(e) => Err(e.clone()),
        };
        let (places, count) = match located {
            Ok(found) => found,
            Err(e) if edits.len() == 1 => return Err(e),
            Err(e) => {
                let k = edits[..i - 1].iter().filter(|(earlier, _)| earlier == key).count();
                let after = if k > 0 {
                    format!(
                        " (old_text is matched against the text with the {k} earlier edit(s) to it applied; line \
                         numbers refer to the text before this call)"
                    )
                } else {
                    String::new()
                };
                return Err(DomainError::invalid(failed_edit(i, edits.len(), &e, &after)));
            }
        };
        current.insert(key.clone(), splice(&text, &places));
        origin.follow(&text, &places, i);
        *counts.entry(key.clone()).or_default() += count;
    }
    Ok((current, counts))
}

/// after 里改动过的行（各带前后 context 行，相邻的合并）的 `[起, 止)` 行号范围，0 起算。按前后文本比对得出，与替换的
/// 先后次序无关（后一处替换可能挪动前一处的位置）。纯删除的地方只剩上下文行；整个文件被删空时为空列表。
pub fn changed_ranges(before: &str, after: &str, context: usize) -> Vec<(usize, usize)> {
    let (a, b) = (split_lines(before), split_lines(after));
    similar::group_diff_ops(similar::capture_diff_slices(similar::Algorithm::Myers, &a, &b), context)
        .into_iter()
        .filter_map(|group| {
            let (start, end) = (group[0].new_range().start, group[group.len() - 1].new_range().end);
            (start < end).then_some((start, end))
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn apply(text: &str, edits: Vec<Change>) -> Result<String, String> {
        let texts = HashMap::from([("f".to_owned(), text.to_owned())]);
        let edits: Vec<_> = edits.into_iter().map(|e| ("f".to_owned(), Ok(e))).collect();
        apply_each(&texts, &edits, |k| k.clone()).map(|(c, _)| c["f"].clone()).map_err(|e| e.message)
    }

    fn lines(start: usize, end: usize, anchor: &str, new: &str) -> Change {
        Change::Lines { start, end, anchor: anchor.into(), new: new.into() }
    }

    fn replace(old: &str, new: &str) -> Change {
        Change::Replace { old: old.into(), new: new.into(), every: false }
    }

    #[test]
    fn line_numbers_refer_to_the_original_text() {
        let text = "a\nb\nc\nd\n";
        // 第一处在第 1 行后插入两行，第二处仍按原文的第 3 行定位
        let out = apply(text, vec![replace("a\n", "a\nx\ny\n"), lines(3, 3, "c", "C")]).unwrap();
        assert_eq!(out, "a\nx\ny\nb\nC\nd\n");
        // 碰到前面改过的行就拒绝
        let err = apply(text, vec![replace("b", "B"), lines(2, 3, "b", "Z")]).unwrap_err();
        assert_eq!(
            err,
            "edit 2 of 2 (old_text is matched against the text with the 1 earlier edit(s) to it applied; line numbers \
             refer to the text before this call): lines 2-3 overlap edit 1, which already changed that part; merge the \
             two into one edit; nothing was written"
        );
    }

    #[test]
    fn anchors_must_be_the_whole_line() {
        let err = apply("first line\nsecond\n", vec![lines(1, 1, "first", "x")]).unwrap_err();
        assert!(
            err.starts_with("anchor matches only the beginning of line 1 of f; lines 1-2 are now:\n     1\tfirst line")
        );
        assert_eq!(apply("a\nb", vec![lines(2, 2, " b ", "B")]).unwrap(), "a\nB");
        assert_eq!(apply("a\nb\n", vec![lines(1, 1, "a", "")]).unwrap(), "b\n");
    }

    #[test]
    fn replace_all_counts_every_occurrence() {
        let texts = HashMap::from([("f".to_owned(), "x x x".to_owned())]);
        let (out, counts) = apply_each(
            &texts,
            &[("f".to_owned(), Ok(Change::Replace { old: "x".into(), new: "y".into(), every: true }))],
            |k| k.clone(),
        )
        .unwrap();
        assert_eq!((out["f"].as_str(), counts["f"]), ("y y y", 3));
    }

    #[test]
    fn changed_ranges_keep_context() {
        let before: String = (1..=20).map(|i| format!("{i}\n")).collect();
        let after = before.replace("5\n", "five\n").replace("16\n", "");
        assert_eq!(changed_ranges(&before, &after, 2), vec![(2, 7), (12, 17)]);
        assert_eq!(changed_ranges("a\n", "", 2), Vec::<(usize, usize)>::new());
        assert_eq!(changed_ranges("a\nb\n", "a\nb\n", 2), Vec::<(usize, usize)>::new());
        assert_eq!(changed_ranges("}\n}\nx\n", "}\n}\n}\nx\n", 0).len(), 1);
    }
}

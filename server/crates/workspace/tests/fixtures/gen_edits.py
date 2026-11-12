"""生成 edits.json：用 Python 版（src/muliao/workspace/file_edit.py）对一批随机的多处修改算出结果，Rust 版的测试
（tests/python_parity.rs）逐条比对，证明两版的定位、拼接、报错与改动范围逐字一致。Python 版删除后此脚本作废，
edits.json 照常用。

    cd <仓库根> && uv run python server/crates/workspace/tests/fixtures/gen_edits.py
"""

import json
import random
import sys
from pathlib import Path

sys.path.insert(0, "src")

from muliao.errors import InvalidInput  # noqa: E402
from muliao.workspace import file_edit  # noqa: E402

WORDS = ["", "}", "a", "b c", "  x = 1", "if y:", "}", "中文", "a"]


def text(rng: random.Random) -> str:
    lines = [rng.choice(WORDS) for _ in range(rng.randint(0, 12))]
    end = rng.choice(["\n", "\n", "\r\n"])
    body = end.join(lines)
    return body + end if lines and rng.random() < 0.7 else body


def edit(rng: random.Random, t: str) -> dict:
    lines = t.splitlines()
    if lines and rng.random() < 0.5:
        start = rng.randint(1, len(lines) + (rng.random() < 0.1))
        end = start + rng.choice([0, 0, 1, 2, -1]) if rng.random() < 0.9 else len(lines) + 1
        anchor = lines[start - 1] if start <= len(lines) and rng.random() < 0.85 else rng.choice(WORDS)
        new = rng.choice(["", "N", "N\n", "N1\nN2", "}\n}"])
        return {"lines": [start, end, anchor, new]}
    if t and rng.random() < 0.8:
        i = rng.randint(0, len(t) - 1)
        old = t[i : i + rng.randint(1, 30)]
    else:
        old = rng.choice(WORDS)
    return {"replace": [old, rng.choice(["", "R", "R\n", "}\nR"]), rng.random() < 0.2]}


def change(e: dict) -> file_edit.Edit:
    if "lines" in e:
        return file_edit.LineRange(*e["lines"])
    return file_edit.Replace(*e["replace"])


def main() -> None:
    rng = random.Random(681)
    cases = []
    for _ in range(1000):
        texts = {k: text(rng) for k in ("f", "g")}
        edits = []
        for _ in range(rng.choice([1, 1, 2, 2, 3, 4])):
            key = rng.choice(["f", "f", "g"])
            edits.append([key, edit(rng, texts[key])])
        case = {"texts": texts, "edits": edits}
        try:
            after, counts = file_edit.apply_each(texts, [(k, change(e)) for k, e in edits])
        except InvalidInput as err:
            case["error"] = str(err)
        else:
            case["after"] = after
            case["counts"] = counts
            case["ranges"] = {k: file_edit.changed_ranges(texts[k], after[k], 2) for k in after}
        cases.append(case)
    out = Path(__file__).with_name("edits.json")
    out.write_text(json.dumps(cases, ensure_ascii=False, separators=(",", ":")) + "\n")
    print(f"{len(cases)} cases, {sum('error' in c for c in cases)} errors -> {out}")


if __name__ == "__main__":
    main()

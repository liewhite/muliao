"""类型标注要被检查才算约束：pyright（配置在 pyproject.toml 的 [tool.pyright]）对 src 报的错都要清零。

确实是检查器看不懂的写法（运行时生成的类型、第三方库的标注），在那一行写 `# pyright: ignore[<规则>]` 和原因，
不放宽全局配置。"""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_src_type_checks():
    done = subprocess.run(
        [sys.executable, "-m", "pyright"], cwd=ROOT, capture_output=True, text=True, timeout=300
    )
    assert done.returncode == 0, done.stdout[-8000:] + done.stderr[-2000:]

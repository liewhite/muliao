"""提交的接口描述（web/openapi.json，前端类型由它生成）与当前接口一致。"""

import json
from pathlib import Path

from muliao.api.app import api_schema

SNAPSHOT = Path(__file__).parent.parent / "web" / "openapi.json"


def test_the_committed_openapi_snapshot_matches_the_api():
    # 不一致时重新生成：uv run python -m muliao.api.app > web/openapi.json，再在 web/ 里 npm run gen:api
    assert json.loads(SNAPSHOT.read_text()) == api_schema()

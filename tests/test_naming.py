import unicodedata

import pytest

from muliao.agents.naming import normalize_agent_name
from muliao.errors import InvalidInput


@pytest.mark.parametrize(
    "name", ["助理", "Slack 分诊", "研究/资料", "Alpha", "日本語", "a", "表情😀", "../etc", "x" * 64]
)
def test_any_visible_text_is_a_valid_display_name(name):
    # 名称只用于展示，不进入路径或标识
    assert normalize_agent_name(name) == name


@pytest.mark.parametrize("name", ["", "   ", "x" * 65, "a\nb", "a\x00b"])
def test_rejects_empty_too_long_or_control_characters(name):
    with pytest.raises(InvalidInput):
        normalize_agent_name(name)


def test_trims_and_normalizes_to_nfc():
    decomposed = unicodedata.normalize("NFD", "café")
    assert decomposed != "café"
    assert normalize_agent_name(f"  {decomposed} ") == "café"

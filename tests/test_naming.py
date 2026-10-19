import unicodedata

import pytest

from muliao.errors import InvalidInput
from muliao.naming import normalize_agent_name


@pytest.mark.parametrize("name", ["助理", "slack-分诊", "研究_2", "alpha", "日本語", "a"])
def test_accepts_unicode_names(name):
    assert normalize_agent_name(name) == name


@pytest.mark.parametrize(
    "name", ["", "Alpha", "../etc", "a/b", "a.b", "-lead", "has space", "x" * 65, "表情😀", "..", "a\\b"]
)
def test_rejects_unsafe_or_ambiguous_names(name):
    with pytest.raises(InvalidInput):
        normalize_agent_name(name)


def test_normalizes_to_nfc_so_one_name_maps_to_one_agent():
    decomposed = unicodedata.normalize("NFD", "café")
    assert decomposed != "café"
    assert normalize_agent_name(decomposed) == "café"

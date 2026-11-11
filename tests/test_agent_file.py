"""agent.toml 的格式（agents.agent_file）：界面、工具与人手改文件走同一套规则。"""

import pytest

from muliao.agents import agent_file
from muliao.errors import InvalidInput

BASE = 'id = "0123456789ab"\ndescription = "d"\nmodel = "m:x"\n'


@pytest.mark.parametrize("group", ["   ", "", " ops", "ops ", "x" * 65])
def test_a_group_that_is_blank_padded_or_too_long_is_rejected_in_the_file_itself(group):
    """#613（#578 残余）：分组规则以前只在界面的入口，手改的文件带空白或 200 字的分组也能读进来。"""
    with pytest.raises(InvalidInput, match="group"):
        agent_file.parse(BASE + f"group = {group!r}\n".replace("'", '"'))


def test_a_group_up_to_the_limit_or_none_is_read():
    assert agent_file.parse(BASE + f'group = "{"x" * 64}"\n').group == "x" * 64
    assert agent_file.parse(BASE).group is None

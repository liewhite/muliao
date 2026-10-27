"""agent id 的格式：唯一的定义处（生成与校验都用它）。"""

import re
import uuid

# 12 位小写十六进制，短到模型能可靠复制，用作主键、目录名与工具参数
_LENGTH = 12
AGENT_ID = re.compile(rf"^[0-9a-f]{{{_LENGTH}}}$")


def new_agent_id() -> str:
    return uuid.uuid4().hex[:_LENGTH]


# 全系统指代用户的那个值（agents 目录定义）：做事的人（actor，如版本的改的人、issue 时间线）、issue 的指派人等处与 agent
# id 并列出现。不是 12 位十六进制，不会与 agent id 相同；agent 名称也不能取它（agents.naming.new_agent_name）
USER = "user"

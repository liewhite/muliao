"""agent id 的格式：唯一的定义处（生成与校验都用它）。"""

import re
import uuid

# 12 位小写十六进制，短到模型能可靠复制，用作主键、目录名与工具参数
_LENGTH = 12
AGENT_ID = re.compile(rf"^[0-9a-f]{{{_LENGTH}}}$")


def new_agent_id() -> str:
    return uuid.uuid4().hex[:_LENGTH]

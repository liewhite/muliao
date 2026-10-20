"""全部领域的表：Alembic 与迁移测试从这里拿到完整的 metadata（各领域的表定义在各自的 tables 模块里）。"""

from muliao.agents import tables as agents  # noqa: F401
from muliao.delegations import tables as delegations  # noqa: F401
from muliao.files import tables as files  # noqa: F401
from muliao.jobs import tables as jobs  # noqa: F401
from muliao.notifications import tables as notifications  # noqa: F401
from muliao.platform import tables as platform  # noqa: F401
from muliao.reflection import tables as reflection  # noqa: F401
from muliao.sessions import tables as sessions  # noqa: F401
from muliao.triggers import tables as triggers  # noqa: F401
from muliao.vault import tables as vault  # noqa: F401

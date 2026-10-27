import os
from collections.abc import AsyncIterator, Callable
from pathlib import Path

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models import Model
from pydantic_ai.tools import Tool

from muliao.agent_runtime.llm import check_thinking
from muliao.api.app import create_app
from muliao.config import Settings
from muliao.container import Assembly, assemble
from muliao.platform.db import migrate
from muliao.schema import single_domain_writes
from tests.scripted import scripted

os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")


# 测试用 anyio 的 pytest 插件运行：异步 fixture 与测试在同一个任务里，任务组可以跨 fixture 的 yield（Assembly
# 与 JobRunner 的 running 就是这样）。协程测试由 anyio_mode = auto 收集（pyproject），在 asyncio 后端上运行；
# 用到异步 fixture 的测试必须是协程
@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class ModelSlot:
    """测试中按 agent 的 model 字符串返回预设的模型对象；未预设的返回固定回复 "ok" 的模型。"""

    def __init__(self) -> None:
        self.models: dict[str, Model] = {}
        self._default = scripted(lambda _m, _i: ModelResponse(parts=[TextPart("ok")]))

    def __call__(self, name: str) -> Model:
        return self.models.get(name, self._default)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings.model_validate(
        {
            "data_dir": tmp_path / "data",
            "server": {"public_url": "http://test"},
            "auth": {"password": "password1", "secret_key": "k" * 32},
            "vault": {"key": Fernet.generate_key().decode()},
            "push": {"vapid_subject": "mailto:test@example.com"},
            "llm": {
                "default_model": "fake:default",
                "providers": {"fake": {"type": "anthropic", "api_key": "unused"}},
            },
            "runtime": {"task_retry_base_seconds": 0.01, "task_retry_max_delay_seconds": 0.04},
        }
    )


@pytest.fixture
def models() -> ModelSlot:
    return ModelSlot()


@pytest.fixture
async def container(settings: Settings, models: ModelSlot) -> AsyncIterator[Assembly]:
    c = assemble(
        settings,
        resolve_model=models,
        # 不查 provider 配置（测试模型名不带 provider），思考档位按槽位里的模型照实检查
        model_checker=lambda m, t: check_thinking(m, models(m), t),
        check_writes=single_domain_writes,
    )
    migrate(settings.db_path)
    # 后台任务的监督者需要它的任务组（服务里由 Assembly.running 打开）；worker 等循环由测试自己驱动
    async with c.jobs.running():
        yield c
    await c.db.engine.dispose()


async def restart(container: Assembly) -> None:
    """模拟进程重启后的启动恢复（服务里在 Container.running 的启动阶段、任何循环开始之前执行）。"""
    await container.worker.recover()


@pytest.fixture
def make_agent(container: Assembly) -> Callable:
    """创建 agent 并返回它；测试通过返回值的 .id 引用它（id 由系统生成）。"""
    from muliao.agents.service import AgentSpec

    async def _make(
        name: str = "alpha",
        *,
        model: str = "test",
        description: str = "test agent",
        roles: tuple[str, ...] = (),
    ):
        return await container.agents.create(
            AgentSpec(name=name, description=description, model=model, roles=roles), actor="user"
        )

    return _make


def session_tools(assembly: Assembly) -> dict[str, Tool]:
    """有会话的运行可用的全部静态工具（Toolbox），按工具名。"""
    toolbox = assembly.consumer._toolbox  # noqa: SLF001
    return {name: tool for ts in toolbox.toolsets for name, tool in ts.tools.items()}


def app_of(assembly: Assembly, web_dir: Path) -> FastAPI:
    """服务入口（__main__）同样的装配：HTTP 层只拿 container 与它用到的配置。"""
    settings = assembly.settings
    return create_app(assembly.container, web_dir, auth=settings.auth, public_url=settings.server.public_url)


@pytest.fixture
async def client(container: Assembly, tmp_path: Path) -> AsyncIterator[httpx.AsyncClient]:
    app = app_of(container, tmp_path / "no-web")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def login(client: httpx.AsyncClient) -> None:
    resp = await client.post("/api/auth/login", json={"password": "password1"})
    assert resp.status_code == 204


async def share(container: Assembly, *rules: str) -> None:
    """把条目加进 base 角色（每个 agent 都能用），即原来的「公共」条目。"""
    from muliao.agents.api import RoleSpec

    base = await container.distribution.get("base")
    allow = [*base.rules.allow, *rules]
    await container.distribution.update(
        RoleSpec("base", base.description, allow, list(base.rules.deny)), actor="user"
    )


async def owned(container: Assembly, kind: str, owner: str | None) -> list[str]:
    """owner 名下（None 为平台的）这一种条目的名字，按名称（条目池 ItemCatalog.catalog）。"""
    return [e.name for e in await container.catalog.catalog() if e.kind == kind and e.owner == owner]


async def history(container: Assembly, subject, name: str, limit: int):  # noqa: ANN001, ANN201
    """一个对象的版本，新的在前（agents.revisions.history_in）。"""
    from muliao.agents.revisions import history_in

    async with container.db.transaction() as s:
        return await history_in(s, subject, name, limit)

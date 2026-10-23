import asyncio
import os
import shutil
from collections.abc import AsyncIterator, Callable
from pathlib import Path

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models import Model

from muliao.agents.resources import ResourceRepo
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


async def _no_owner_check(_owner: str | None) -> None:
    """模板仓库只初始化、不写资源。"""


@pytest.fixture(scope="session")
def resources_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """初始化过一次的资源仓库（init、config、初始提交，与生产的 ResourceRepo.ensure 相同）。

    每个用例新建仓库要跑 4 个 git 子进程，测试总耗时里占了很大一块；用例改用它的副本（见 seed_resources），
    ensure() 看到 .git 已存在就直接跳过。
    """
    root = tmp_path_factory.mktemp("resources-template") / "resources"
    asyncio.run(ResourceRepo(root, _no_owner_check).ensure())
    return root


@pytest.fixture
def seed_resources(resources_template: Path) -> Callable[[Path], Path]:
    """把模板仓库复制到 path（不能已存在），返回 path。"""

    def _seed(path: Path) -> Path:
        shutil.copytree(resources_template, path)
        return path

    return _seed


@pytest.fixture
def settings(tmp_path: Path, seed_resources: Callable[[Path], Path]) -> Settings:
    seed_resources(tmp_path / "data" / "resources")
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
        settings, resolve_model=models, model_checker=lambda _m: None, check_writes=single_domain_writes
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

    async def _make(name: str = "alpha", *, model: str = "test", description: str = "test agent"):
        return await container.agents.create(
            AgentSpec(
                name=name,
                description=description,
                model=model,
            ),
            author="user",
        )

    return _make


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

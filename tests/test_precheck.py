"""触发器的前置检查：有事才调用 agent；changed 模式比较上一次输出（首轮只记基线）；检查出错记录并只告警一次。"""

import pytest

from muliao.errors import NotFound
from muliao.models import TaskKind
from muliao.triggers.precheck import Precheck, PrecheckFailed, decide
from tests.conftest import login
from tests.test_flow import mail_of, tasks_of
from tests.test_triggers import _make_due
from tests.test_webhooks import _bearer, _create


def test_output_mode_fires_on_any_output():
    p = Precheck(command="x")
    assert decide(p, "new message\n", None, 100).event == "new message\n"
    assert decide(p, " \n", "anything", 100).event is None


def test_changed_mode_records_a_baseline_first_then_fires_only_on_change_with_a_diff():
    p = Precheck(command="x", fire_when="changed")
    first = decide(p, "price 100\n", None, 1000)
    assert first.event is None and first.baseline
    assert decide(p, "price 100\n", "price 100\n", 1000).event is None
    event = decide(p, "price 120\n", "price 100\n", 1000).event
    assert event is not None
    assert "-price 100" in event and "+price 120" in event and "<output>\nprice 120\n" in event
    with pytest.raises(PrecheckFailed, match="event limit"):
        decide(p, "price 120\n", "price 100\n", 20)


async def _schedule(container, agent_id: str, precheck: Precheck):
    trigger = await container.triggers.create(
        name="watch",
        agent_id=agent_id,
        kind="schedule",
        prompt="Handle the change.",
        config={"cron": "* * * * *"},
        precheck=precheck,
    )
    workspace = container.agents.workspace(agent_id)
    workspace.mkdir(parents=True, exist_ok=True)
    return trigger, workspace


async def _tick(container, trigger_id: int) -> None:
    await _make_due(container, trigger_id)
    await container.schedules.fire_due()


async def test_schedule_runs_the_agent_only_when_the_precheck_has_output(container, make_agent):
    alpha = (await make_agent()).id
    trigger, workspace = await _schedule(
        container, alpha, Precheck(command="cat inbox.txt 2>/dev/null || true")
    )

    await _tick(container, trigger.id)
    assert await tasks_of(container, TaskKind.TURN) == []
    checked = await container.triggers.get_by_name("watch")
    assert checked.last_checked_at is not None and checked.last_session_id is None

    (workspace / "inbox.txt").write_text("alice: deploy is broken\n")
    await _tick(container, trigger.id)
    turns = await tasks_of(container, TaskKind.TURN)
    [prompt] = [m.content for m in await mail_of(container)]
    assert len(turns) == 1
    assert prompt.endswith("<event>\nalice: deploy is broken\n\n</event>")


async def test_changed_precheck_skips_the_baseline_and_unchanged_runs(container, make_agent):
    alpha = (await make_agent()).id
    trigger, workspace = await _schedule(
        container, alpha, Precheck(command="cat price.txt", fire_when="changed")
    )
    (workspace / "price.txt").write_text("ETH 2400\n")

    await _tick(container, trigger.id)  # 基线
    await _tick(container, trigger.id)  # 没变
    assert await tasks_of(container, TaskKind.TURN) == []

    (workspace / "price.txt").write_text("ETH 2600\n")
    await _tick(container, trigger.id)
    turns = await tasks_of(container, TaskKind.TURN)
    [prompt] = [m.content for m in await mail_of(container)]
    assert len(turns) == 1 and "-ETH 2400" in prompt and "+ETH 2600" in prompt

    # 换了检查就从新的基线开始
    await container.triggers.update(
        trigger.id, precheck=Precheck(command="cat price.txt; echo v2", fire_when="changed")
    )
    await _tick(container, trigger.id)
    assert len(await tasks_of(container, TaskKind.TURN)) == 1


async def test_failing_precheck_is_recorded_alerts_once_and_keeps_the_trigger_enabled(container, make_agent):
    alpha = (await make_agent()).id
    trigger, workspace = await _schedule(
        container, alpha, Precheck(command="test -f ok || { echo 'no token' >&2; exit 3; }")
    )

    await _tick(container, trigger.id)
    await _tick(container, trigger.id)

    failed = await container.triggers.get_by_name("watch")
    assert failed.enabled and failed.last_error is not None and "exited with 3: no token" in failed.last_error
    assert await tasks_of(container, TaskKind.TURN) == []
    assert len(await tasks_of(container, TaskKind.DELIVER)) == 1
    # 告警留在通知页里：检查恢复、last_error 清除之后仍然看得到
    [alert] = await container.notifications.list(10)
    assert "watch" in alert.title and "no token" in alert.body and alert.session_id is None

    (workspace / "ok").write_text("")
    await _tick(container, trigger.id)
    assert (await container.triggers.get_by_name("watch")).last_error is None


async def test_a_failing_step_inside_a_pipeline_fails_the_precheck(container, make_agent):
    alpha = (await make_agent()).id
    # 像 curl 失败而 jq 对空输入照样退出 0：不能被当成「没事」
    trigger, _ = await _schedule(container, alpha, Precheck(command="false | cat"))
    await _tick(container, trigger.id)
    assert "exited with 1" in (await container.triggers.get_by_name("watch")).last_error


async def test_precheck_secrets_must_exist_when_the_trigger_is_created(container, make_agent):
    alpha = (await make_agent()).id
    with pytest.raises(NotFound, match="NOPE"):
        await _schedule(container, alpha, Precheck(command="true", secrets=["NOPE"]))


async def test_webhook_precheck_filters_the_event_and_hides_failures_from_the_caller(
    client, container, make_agent
):
    alpha = (await make_agent("alpha")).id
    await login(client)
    trigger, token = await _create(client, alpha)
    resp = await client.patch(
        f"/api/triggers/{trigger['id']}",
        json={"precheck": {"command": "grep -i urgent || true"}},
    )
    assert resp.status_code == 200 and resp.json()["precheck"]["command"] == "grep -i urgent || true"

    quiet = await client.post("/api/hooks/github", content="routine update\n", headers=_bearer(token))
    assert quiet.status_code == 202 and quiet.json()["session_id"] is None
    loud = await client.post("/api/hooks/github", content="ok\nURGENT: prod down\n", headers=_bearer(token))
    assert loud.json()["session_id"] is not None
    turns = await tasks_of(container, TaskKind.TURN)
    [prompt] = [m.content for m in await mail_of(container)]
    assert len(turns) == 1 and "URGENT: prod down" in prompt
    assert "routine" not in prompt and "ok\n" not in prompt

    await client.patch(
        f"/api/triggers/{trigger['id']}", json={"precheck": {"command": "echo secret-ish >&2; exit 1"}}
    )
    broken = await client.post("/api/hooks/github", content="x", headers=_bearer(token))
    assert broken.status_code == 502 and "secret-ish" not in broken.text

    # 不给 precheck 字段 = 不变；给 null = 去掉
    assert (await client.patch(f"/api/triggers/{trigger['id']}", json={"enabled": True})).json()[
        "precheck"
    ] is not None
    assert (await client.patch(f"/api/triggers/{trigger['id']}", json={"precheck": None})).json()[
        "precheck"
    ] is None


async def test_manual_run_goes_through_the_precheck(client, container, make_agent):
    alpha = (await make_agent()).id
    trigger, _ = await _schedule(container, alpha, Precheck(command="true"))
    await login(client)
    resp = await client.post(f"/api/triggers/{trigger.id}/run")
    assert resp.status_code == 202 and resp.json()["session_id"] is None
    assert [t.status for t in await tasks_of(container, TaskKind.TURN)] == []


async def test_a_precheck_with_secrets_can_be_changed_and_unknown_secrets_are_refused(container, make_agent):
    alpha = (await make_agent()).id
    await container.vault.set(alpha, "API_KEY", "k-1")
    trigger, _ = await _schedule(container, alpha, Precheck(command="true"))

    changed = await container.triggers.update(
        trigger.id, precheck=Precheck(command='test -n "$API_KEY" && echo ok', secrets=["API_KEY"])
    )
    assert changed.precheck["secrets"] == ["API_KEY"]
    with pytest.raises(NotFound, match="MISSING"):
        await container.triggers.update(trigger.id, precheck=Precheck(command="true", secrets=["MISSING"]))

    await _tick(container, trigger.id)
    assert len(await tasks_of(container, TaskKind.TURN)) == 1


async def test_a_change_too_large_to_hand_over_is_recorded_and_alerted(container, make_agent):
    alpha = (await make_agent()).id
    trigger, workspace = await _schedule(
        container, alpha, Precheck(command="cat big.txt", fire_when="changed")
    )
    container.triggers._precheck._max_bytes = 100  # noqa: SLF001  测试里调小事件上限
    (workspace / "big.txt").write_text("a" * 60)
    await _tick(container, trigger.id)  # 基线：60 字节在上限内
    (workspace / "big.txt").write_text("b" * 60)  # diff 加输出超过 100 字节
    await _tick(container, trigger.id)

    failed = await container.triggers.get_by_name("watch")
    assert failed.last_error is not None and "event limit" in failed.last_error
    assert len(await tasks_of(container, TaskKind.DELIVER)) == 1
    assert await tasks_of(container, TaskKind.TURN) == []


async def test_a_failing_precheck_on_a_manual_run_is_a_502_with_the_reason(client, container, make_agent):
    alpha = (await make_agent()).id
    trigger, _ = await _schedule(container, alpha, Precheck(command="echo boom >&2; exit 4"))
    await login(client)
    resp = await client.post(f"/api/triggers/{trigger.id}/run")
    assert resp.status_code == 502 and "boom" in resp.json()["detail"]


async def test_enabling_an_enabled_trigger_keeps_its_error_and_does_not_alert_again(container, make_agent):
    alpha = (await make_agent()).id
    trigger, _ = await _schedule(container, alpha, Precheck(command="exit 3"))
    await _tick(container, trigger.id)
    await container.triggers.update(trigger.id, enabled=True)
    assert (await container.triggers.get_by_name("watch")).last_error is not None
    await _tick(container, trigger.id)
    assert len(await tasks_of(container, TaskKind.DELIVER)) == 1


async def test_changing_the_precheck_clears_the_old_commands_error(container, make_agent):
    """旧命令的失败不记到新配置上：新命令也失败时要再告警一次。"""
    alpha = (await make_agent()).id
    trigger, _ = await _schedule(container, alpha, Precheck(command="exit 3"))
    await _tick(container, trigger.id)
    await container.triggers.update(trigger.id, precheck=Precheck(command="exit 5"))
    assert (await container.triggers.get_by_name("watch")).last_error is None
    await _tick(container, trigger.id)
    assert len(await tasks_of(container, TaskKind.DELIVER)) == 2

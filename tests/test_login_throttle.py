"""登录失败按来源 IP 退避（#540）。"""

from muliao.api.auth import LoginThrottle
from tests.conftest import login

FREE = LoginThrottle.FREE_FAILURES


def _fail(t: LoginThrottle, ip: str, times: int, now: float = 0.0) -> None:
    for _ in range(times):
        t.failed(ip, now)


def test_first_failures_are_free_then_backoff_starts():
    t = LoginThrottle()
    _fail(t, "1.1.1.1", FREE - 1)
    assert t.retry_after("1.1.1.1", 0.0) == 0
    _fail(t, "1.1.1.1", 1)
    assert t.retry_after("1.1.1.1", 0.0) == LoginThrottle.BASE_DELAY


def test_backoff_expires_and_doubles_per_further_failure():
    t = LoginThrottle()
    _fail(t, "1.1.1.1", FREE)
    base = LoginThrottle.BASE_DELAY
    assert t.retry_after("1.1.1.1", base - 1) == 1
    assert t.retry_after("1.1.1.1", base) == 0
    t.failed("1.1.1.1", base)
    assert t.retry_after("1.1.1.1", base) == 2 * base


def test_backoff_is_capped():
    t = LoginThrottle()
    _fail(t, "1.1.1.1", FREE + 30)
    assert t.retry_after("1.1.1.1", 0.0) == LoginThrottle.MAX_DELAY


def test_ips_are_counted_separately():
    t = LoginThrottle()
    _fail(t, "1.1.1.1", FREE)
    assert t.retry_after("1.1.1.1", 0.0) > 0
    assert t.retry_after("2.2.2.2", 0.0) == 0


def test_success_clears_failures():
    t = LoginThrottle()
    _fail(t, "1.1.1.1", FREE - 1)
    t.succeeded("1.1.1.1")
    _fail(t, "1.1.1.1", FREE - 1)
    assert t.retry_after("1.1.1.1", 0.0) == 0


def test_failures_are_forgotten_after_quiet_period():
    t = LoginThrottle()
    _fail(t, "1.1.1.1", FREE + 3)
    later = LoginThrottle.FORGET_AFTER
    assert t.retry_after("1.1.1.1", later) == 0
    # 遗忘后从头计数：再错一次仍在免费次数内
    t.failed("1.1.1.1", later)
    assert t.retry_after("1.1.1.1", later) == 0


def test_forgotten_entries_are_dropped():
    t = LoginThrottle()
    _fail(t, "1.1.1.1", 1)
    t.failed("2.2.2.2", LoginThrottle.FORGET_AFTER)
    assert set(t._failures) == {"2.2.2.2"}


def test_refailing_ip_is_not_dropped_by_pruning():
    # 1.1.1.1 先失败、后又失败：清理按最后一次失败的时间算，不能因为它最早出现就被删
    t = LoginThrottle()
    t.failed("1.1.1.1", 0.0)
    t.failed("2.2.2.2", 1.0)
    t.failed("1.1.1.1", 2.0)
    t.failed("3.3.3.3", LoginThrottle.FORGET_AFTER + 1.5)
    assert set(t._failures) == {"1.1.1.1", "3.3.3.3"}


async def test_login_rejects_even_right_password_while_backing_off(client):
    for _ in range(FREE):
        assert (await client.post("/api/auth/login", json={"password": "nope"})).status_code == 401
    resp = await client.post("/api/auth/login", json={"password": "password1"})
    assert resp.status_code == 429
    assert 0 < int(resp.headers["Retry-After"]) <= LoginThrottle.BASE_DELAY


async def test_login_success_resets_failures(client):
    for _ in range(FREE - 1):
        assert (await client.post("/api/auth/login", json={"password": "nope"})).status_code == 401
    await login(client)
    for _ in range(FREE - 1):
        assert (await client.post("/api/auth/login", json={"password": "nope"})).status_code == 401
    await login(client)

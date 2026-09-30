"""P0-2 适配器韧性单测：重试 / 指数退避 / 熔断 / 降级。

全部离线、零等待（sleep 注入为 no-op）、零网络（HTTP 层被替身接管）。

覆盖的核心契约：
- `RetryPolicy.max_retries` 是**首次之外**的次数 → 总尝试数 = max_retries + 1；
- 退避指数增长且有上限，避免一次问答被拖成分钟级；
- 熔断连续失败达阈值开路，**开路期不再发下游调用**（快速失败）；
- 冷却后放一次半开探测，成功闭合、失败重新计时；
- 重试耗尽**不抛异常**，改为降级（`degraded=True`），主链路不崩；
- **失败不写缓存**（否则一次抖动被 TTL 放大成持续 30s 的错误）；
- 降级卡片在归一化 / 模板合成 / LLM 提示词三处都不会 KeyError。

P0-2 收尾（真实 endpoint 联调）补的契约：
- **4xx（除 408/429）是确定性失败**：不重试、不计熔断；
- `404` 是**第三种终态** `not_found`，与 `degraded` 严格区分；
- `RetryPolicy.call(retryable=)` 的判定在**任一次**失败上生效，且默认 `None` 保持旧语义。
"""

from __future__ import annotations

import urllib.error

import pytest

from kb_mcp_server import adapters
from kb_mcp_server.extensions import (
    CIRCUIT_CLOSED,
    CIRCUIT_HALF_OPEN,
    CIRCUIT_OPEN,
    CircuitBreaker,
    RetryPolicy,
)


# --------------------------------------------------------------------------- #
# 测试替身
# --------------------------------------------------------------------------- #
def _install_http(monkeypatch, fail_first: int = 0, payload: dict | None = None):
    """把 adapters 的 HTTP 取数替换为可控桩：前 fail_first 次抛错，之后成功。

    返回 counters 字典，`n` 为实际发起过的 HTTP 次数。
    """
    counters = {"n": 0}
    body = payload or {"status": "已发货", "carrier": "顺丰", "eta": "2026-09-30"}

    def fake_http(url, api_key, timeout, scheme="bearer",
                  auth_header="Authorization", auth_query=""):
        counters["n"] += 1
        if counters["n"] <= fail_first:
            raise OSError(f"connection refused #{counters['n']}")
        return body

    monkeypatch.setattr(adapters, "_http_get_json", fake_http)
    return counters


def _order_adapter(monkeypatch, *, policy: RetryPolicy, fail_first: int = 0, ttl: int = 30):
    """构造一个「已启用真实模式」的订单适配器（无真实网络、无真实等待）。"""
    counters = _install_http(monkeypatch, fail_first=fail_first)
    a = adapters.OrderStatusAdapter(
        base_url="http://order.test", path_tpl="/orders/{id}",
        retry_policy=policy, sleep=lambda _s: None,
    )
    a.mock = False          # enabled() 要求 base_url 非空且非 mock
    a.ttl = ttl
    return a, counters


def _install_http_status(monkeypatch, status: int, fail_first: int | None = None):
    """HTTP 层桩：前 `fail_first` 次抛 `HTTPStatusError(status)`，之后成功。

    与 `_install_http` 的区别：那个抛 `OSError`（泛指瞬时网络故障），
    这个抛**带状态码**的失败，用来验证「按状态码决策」的分支。
    `fail_first=None` 表示一直失败（用于「确定性失败」用例）。
    """
    counters = {"n": 0}
    body = {"status": "已发货", "carrier": "顺丰", "eta": "2026-09-30"}
    limit = 10 ** 9 if fail_first is None else fail_first

    def fake_http(url, api_key, timeout, scheme="bearer",
                  auth_header="Authorization", auth_query=""):
        counters["n"] += 1
        if counters["n"] <= limit:
            raise adapters.HTTPStatusError(status, url, "test-injected")
        return body

    monkeypatch.setattr(adapters, "_http_get_json", fake_http)
    return counters


# --------------------------------------------------------------------------- #
# RetryPolicy / CircuitBreaker 本体
# --------------------------------------------------------------------------- #
class TestCircuitBreaker:
    """状态机用显式 now 驱动，完全不依赖真实时钟。"""

    def test_opens_at_threshold_and_half_opens_after_cooldown(self):
        cb = CircuitBreaker(threshold=2, cooldown_s=30)
        assert cb.state(now=100) == CIRCUIT_CLOSED

        cb.record_failure(now=100)
        assert cb.state(now=100) == CIRCUIT_CLOSED, "未达阈值不应开路"
        assert cb.allow(now=100) is True

        cb.record_failure(now=101)
        assert cb.state(now=101) == CIRCUIT_OPEN
        assert cb.allow(now=110) is False, "开路期必须拒绝调用"

        assert cb.state(now=130) == CIRCUIT_OPEN, "冷却未到仍是开路"
        assert cb.state(now=131) == CIRCUIT_HALF_OPEN, "冷却到点应放行半开探测"
        assert cb.allow(now=131) is True

    def test_half_open_failure_restarts_cooldown(self):
        cb = CircuitBreaker(threshold=1, cooldown_s=30)
        cb.record_failure(now=100)
        assert cb.state(now=131) == CIRCUIT_HALF_OPEN

        cb.record_failure(now=131)          # 半开探测又失败
        assert cb.state(now=140) == CIRCUIT_OPEN, "重新计时后不应立刻半开"
        assert cb.state(now=161) == CIRCUIT_HALF_OPEN

    def test_success_closes_circuit(self):
        cb = CircuitBreaker(threshold=1, cooldown_s=30)
        cb.record_failure(now=100)
        cb.record_success()
        assert cb.state(now=100) == CIRCUIT_CLOSED


class TestRetryPolicy:
    def test_delay_is_exponential_and_capped(self):
        p = RetryPolicy(backoff_s=0.5, max_backoff_s=8.0)
        assert p.delay_for(1) == 0.5
        assert p.delay_for(2) == 1.0
        assert p.delay_for(3) == 2.0
        assert p.delay_for(5) == 8.0
        assert p.delay_for(9) == 8.0, "必须封顶，否则指数退避会把一次问答拖成分钟级"

    def test_zero_backoff_means_no_wait(self):
        assert RetryPolicy(backoff_s=0).delay_for(3) == 0.0

    def test_succeeds_after_transient_failures(self):
        calls = {"n": 0}

        def flaky():
            calls["n"] += 1
            if calls["n"] < 3:
                raise OSError("boom")
            return "ok"

        res = RetryPolicy(max_retries=3).call(flaky, sleep=lambda _s: None)
        assert res.ok and res.value == "ok"
        assert res.attempts == 3

    def test_gives_up_and_reports_last_error(self):
        def always_fail():
            raise OSError("down")

        res = RetryPolicy(max_retries=2).call(always_fail, sleep=lambda _s: None)
        assert res.ok is False
        assert res.attempts == 3, "总尝试数 = max_retries + 1"
        assert "OSError" in res.error_text and "down" in res.error_text

    def test_zero_retries_calls_exactly_once(self):
        calls = {"n": 0}

        def always_fail():
            calls["n"] += 1
            raise OSError("down")

        res = RetryPolicy(max_retries=0).call(always_fail, sleep=lambda _s: None)
        assert calls["n"] == 1 and res.attempts == 1 and res.ok is False

    def test_sleep_called_with_backoff_delays(self):
        slept: list[float] = []

        def always_fail():
            raise OSError("down")

        RetryPolicy(max_retries=3, backoff_s=1.0).call(always_fail, sleep=slept.append)
        assert slept == [1.0, 2.0, 4.0]

    def test_short_circuits_when_circuit_open(self):
        calls = {"n": 0}

        def fn():
            calls["n"] += 1
            raise OSError("down")

        p = RetryPolicy(max_retries=0, circuit_breaker=True, cb_threshold=1, cb_cooldown_s=60)
        p.call(fn, sleep=lambda _s: None)          # 第一次失败即开路
        assert calls["n"] == 1

        res = p.call(fn, sleep=lambda _s: None)    # 开路期
        assert calls["n"] == 1, "开路期间不得发起调用"
        assert res.short_circuited and res.attempts == 0
        assert res.ok is False

    def test_stops_retrying_once_circuit_opens_midway(self):
        calls = {"n": 0}

        def fn():
            calls["n"] += 1
            raise OSError("down")

        p = RetryPolicy(max_retries=5, circuit_breaker=True, cb_threshold=2, cb_cooldown_s=60)
        res = p.call(fn, sleep=lambda _s: None)
        assert calls["n"] == 2, "阈值 2 达到后应立即停止后续重试"
        assert res.short_circuited and res.attempts == 2

    def test_breaker_state_is_closed_without_circuit_breaker(self):
        assert RetryPolicy(circuit_breaker=False).breaker_state() == CIRCUIT_CLOSED


# --------------------------------------------------------------------------- #
# 适配器接入：降级、缓存语义
# --------------------------------------------------------------------------- #
class TestAdapterDegradation:
    def test_degrades_instead_of_raising(self, monkeypatch):
        a, counters = _order_adapter(monkeypatch, policy=RetryPolicy(max_retries=2),
                                     fail_first=99)
        r = a.call(order_id="A1")               # 不得抛异常

        assert r["degraded"] is True
        assert r["attempts"] == 3
        assert "OSError" in r["error"]
        assert r["adapter"] == "order_status"
        assert counters["n"] == 3
        assert a.stats["degraded"] == 1 and a.stats["retries"] == 2

    def test_short_circuited_flag_after_open(self, monkeypatch):
        p = RetryPolicy(max_retries=0, circuit_breaker=True, cb_threshold=2, cb_cooldown_s=60)
        a, counters = _order_adapter(monkeypatch, policy=p, fail_first=99)

        a.call(order_id="A1")
        a.call(order_id="A2")
        assert a.policy.breaker_state() == CIRCUIT_OPEN

        before = counters["n"]
        r = a.call(order_id="A3")
        assert counters["n"] == before, "开路后不应再打下游"
        assert r["degraded"] and r["short_circuited"] and r["circuit"] == CIRCUIT_OPEN
        assert a.stats["short_circuited"] == 1

    def test_failures_are_not_cached(self, monkeypatch):
        """一次网络抖动不能被 TTL 放大成持续 30s 的错误。"""
        a, counters = _order_adapter(monkeypatch, policy=RetryPolicy(max_retries=0),
                                     fail_first=1, ttl=60)
        first = a.call(order_id="A1")
        assert first["degraded"] is True

        second = a.call(order_id="A1")
        assert second["degraded"] is False and second["status"] == "已发货"
        assert counters["n"] == 2

    def test_success_is_cached_within_ttl(self, monkeypatch):
        a, counters = _order_adapter(monkeypatch, policy=RetryPolicy(max_retries=0), ttl=60)
        a.call(order_id="A1")
        n_after_first = counters["n"]

        r = a.call(order_id="A1")
        assert counters["n"] == n_after_first, "TTL 内应命中缓存"
        assert r["status"] == "已发货" and r["degraded"] is False

    def test_cached_hit_served_while_circuit_open(self, monkeypatch):
        """缓存与熔断正交：后端已挂但缓存未过期时，仍应正常返回缓存值。"""
        a, _ = _order_adapter(monkeypatch, policy=RetryPolicy(max_retries=0), ttl=60)
        a.call(order_id="A1")                    # 写缓存

        p = RetryPolicy(max_retries=0, circuit_breaker=True, cb_threshold=1, cb_cooldown_s=60)
        p.breaker.record_failure()               # 强制开路
        a.policy = p

        r = a.call(order_id="A1")
        assert r["degraded"] is False and r["status"] == "已发货"

    def test_probe_reports_degraded_as_failure(self, monkeypatch):
        """降级不抛异常，所以 probe 必须显式判定——否则「取数失败」会被误报成自检通过。"""
        a, _ = _order_adapter(monkeypatch, policy=RetryPolicy(max_retries=0), fail_first=99)
        rep = a.probe("TEST1")
        assert rep["ok"] is False
        assert rep["error"] and "OSError" in rep["error"]
        assert rep["circuit"] == CIRCUIT_CLOSED

    def test_mock_mode_is_unaffected(self, monkeypatch):
        """未配置真实 endpoint 时必须是纯 mock，不该走重试/降级路径。"""
        counters = _install_http(monkeypatch, fail_first=99)
        a = adapters.OrderStatusAdapter(base_url="", retry_policy=RetryPolicy(), sleep=lambda _s: None)
        r = a.call(order_id="A1")
        assert r["mock"] is True and not r["degraded"] and counters["n"] == 0


# --------------------------------------------------------------------------- #
# 配置驱动
# --------------------------------------------------------------------------- #
class TestPolicyFromConfig:
    def test_reads_values_from_runtime_config(self, monkeypatch):
        cfg = {
            "KB_API_MAX_RETRIES": "4",
            "KB_API_RETRY_BACKOFF": "0.25",
            "KB_API_CIRCUIT_BREAKER": "1",
            "KB_API_CIRCUIT_THRESHOLD": "3",
            "KB_API_CIRCUIT_COOLDOWN": "15",
        }
        monkeypatch.setattr(adapters, "get_cfg", lambda k, d="": cfg.get(k, d))
        p = adapters._policy_from_cfg(5)
        assert (p.max_retries, p.backoff_s, p.cb_threshold, p.cb_cooldown_s) == (4, 0.25, 3, 15.0)
        assert p.breaker is not None

    def test_bogus_values_fall_back_to_defaults(self, monkeypatch):
        """配置被写成非法值时回落默认——绝不能让 int()/float() 把服务打崩。"""
        cfg = {
            "KB_API_MAX_RETRIES": "abc",
            "KB_API_RETRY_BACKOFF": "xx",
            "KB_API_CIRCUIT_THRESHOLD": "-3",
            "KB_API_CIRCUIT_COOLDOWN": "",
        }
        monkeypatch.setattr(adapters, "get_cfg", lambda k, d="": cfg.get(k, d))
        p = adapters._policy_from_cfg(5)
        assert p.max_retries == 2 and p.backoff_s == 0.5
        assert p.cb_threshold == 5 and p.cb_cooldown_s == 30.0

    def test_circuit_breaker_can_be_disabled(self, monkeypatch):
        monkeypatch.setattr(adapters, "get_cfg",
                            lambda k, d="": "0" if k == "KB_API_CIRCUIT_BREAKER" else d)
        assert adapters._policy_from_cfg(5).breaker is None

    def test_each_adapter_gets_its_own_breaker(self, monkeypatch):
        """熔断是「按后端」的状态：订单 API 挂掉不该让库存查询一起快速失败。"""
        monkeypatch.setattr(adapters, "get_cfg",
                            lambda k, d="": "1" if k == "KB_API_CIRCUIT_BREAKER" else d)
        reg = adapters.build_registry()
        order, inv = reg["order_status"], reg["inventory"]
        assert order.policy.breaker is not inv.policy.breaker

    def test_adapter_status_exposes_circuit_and_degraded_counts(self):
        st = {x["name"]: x for x in adapters.adapter_status()}
        assert "circuit" in st["order_status"]
        assert "degraded_calls" in st["order_status"]


# --------------------------------------------------------------------------- #
# 降级卡片的下游消费（合成层 / 前端）
# --------------------------------------------------------------------------- #
class TestDegradedCardConsumption:
    def test_normalize_live_marks_degraded_card(self):
        cards = adapters.normalize_live([
            {"order_id": "A1", "degraded": True, "adapter": "order_status",
             "error": "OSError: down", "attempts": 3, "circuit": CIRCUIT_CLOSED},
        ])
        assert cards[0]["type"] == "degraded"
        assert cards[0]["attempts"] == 3
        assert cards[0]["order_id"] == "A1"

    def test_degraded_takes_priority_over_order_card(self):
        """降级响应仍带 order_id，若不优先判定会被误判成「订单卡片但状态为空」。"""
        cards = adapters.normalize_live([
            {"order_id": "A1", "status": None, "degraded": True, "adapter": "order_status"},
        ])
        assert len(cards) == 1 and cards[0]["type"] == "degraded"

    def test_normal_cards_still_work(self):
        cards = adapters.normalize_live([
            {"order_id": "A1", "status": "已发货"},
            {"sku": "S1", "stock": 3},
            {"adapter": "inventory", "note": "请提供 SKU"},
        ])
        assert [c["type"] for c in cards] == ["order", "inventory", "prompt"]

    def test_live_line_is_safe_for_every_card_type(self):
        """回归：旧写法 else 分支直接取 c['note']，degraded 卡会 KeyError。"""
        from kb_mcp_server.synthesis import _live_line

        assert "已发货" in _live_line({"type": "order", "order_id": "A1", "status": "已发货"})
        assert "库存" in _live_line({"type": "inventory", "sku": "S1", "stock": 3})
        assert "请提供 SKU" in _live_line({"type": "prompt", "adapter": "inventory",
                                          "note": "请提供 SKU"})
        assert "暂不可用" in _live_line({"type": "degraded", "adapter": "order_status", "attempts": 3})
        assert "熔断" in _live_line({"type": "degraded", "adapter": "order_status",
                                     "short_circuited": True})
        assert _live_line({"type": "unknown"}) == "："      # 未知类型不抛错

    def test_template_synthesis_reports_degraded(self):
        from kb_mcp_server.synthesis import template_synthesis

        _, detail = template_synthesis("订单到哪了", [], [{
            "type": "degraded", "adapter": "order_status", "attempts": 3,
        }])
        assert "【实时数据】" in detail and "暂不可用" in detail


# --------------------------------------------------------------------------- #
# P0-2 收尾：可重试性判定（真实性失败 vs 瞬时失败）
# --------------------------------------------------------------------------- #
class TestRetryableClassification:
    """`is_retryable` 是「这次失败值不值得重试」的唯一裁决点。

    判错的代价不对称：
    - 把瞬时故障判成确定性失败 → 少了本该有的自愈，一次抖动直接降级；
    - 把确定性失败判成瞬时故障 → 白白重试 N 次、拖长延迟、还污染熔断统计。
    """

    def test_4xx_are_not_retryable(self):
        for code in (400, 401, 403, 404, 409, 422):
            assert adapters.is_retryable(adapters.HTTPStatusError(code, "u")) is False, code

    def test_408_and_429_are_retryable(self):
        # 服务端自己的超时 / 限流：退避后重试是对的
        assert adapters.is_retryable(adapters.HTTPStatusError(408, "u")) is True
        assert adapters.is_retryable(adapters.HTTPStatusError(429, "u")) is True

    def test_5xx_and_transport_errors_are_retryable(self):
        assert adapters.is_retryable(adapters.HTTPStatusError(500, "u")) is True
        assert adapters.is_retryable(adapters.HTTPStatusError(503, "u")) is True
        # 无状态码的失败（连接被拒 / 超时 / DNS）一律重试
        assert adapters.is_retryable(OSError("connection refused")) is True
        assert adapters.is_retryable(TimeoutError("timed out")) is True

    def test_http_status_error_carries_status_and_url(self):
        e = adapters.HTTPStatusError(404, "http://x/orders/1", "Not Found")
        assert e.status == 404 and e.url == "http://x/orders/1"
        assert "404" in str(e) and "Not Found" in str(e)


class TestHttpStatusErrorConversion:
    """`_http_get_json` 只把 HTTP 状态类失败转成 `HTTPStatusError`，其余原样透传。"""

    def test_httperror_is_converted(self, monkeypatch):
        def boom(req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, 503, "Service Unavailable", {}, None)

        monkeypatch.setattr("urllib.request.urlopen", boom)
        with pytest.raises(adapters.HTTPStatusError) as ei:
            adapters._http_get_json("http://x/1", None, 5)
        assert ei.value.status == 503 and ei.value.url == "http://x/1"

    def test_transport_error_passes_through_unchanged(self, monkeypatch):
        """非 HTTP 状态类失败不能被包装——上层要靠类型区分「超时」与「状态码」。"""

        def boom(req, timeout=None):
            raise TimeoutError("timed out")

        monkeypatch.setattr("urllib.request.urlopen", boom)
        with pytest.raises(TimeoutError):
            adapters._http_get_json("http://x/1", None, 5)


class TestNonRetryablePolicyContract:
    """`RetryPolicy.call(retryable=)` 的行为契约。"""

    def test_non_retryable_stops_immediately(self):
        calls = {"n": 0}

        def fn():
            calls["n"] += 1
            raise adapters.HTTPStatusError(404, "u")

        res = RetryPolicy(max_retries=5).call(
            fn, sleep=lambda _s: None, retryable=lambda _e: False)
        assert calls["n"] == 1 and res.attempts == 1
        assert res.ok is False and res.non_retryable is True
        assert isinstance(res.error, adapters.HTTPStatusError)

    def test_non_retryable_does_not_trip_breaker(self):
        """确定性失败 ≠ 后端不可用：阈值 1 也不能被它打开熔断。"""
        p = RetryPolicy(max_retries=3, circuit_breaker=True, cb_threshold=1, cb_cooldown_s=60)

        def fn():
            raise adapters.HTTPStatusError(401, "u")

        p.call(fn, sleep=lambda _s: None, retryable=lambda _e: False)
        assert p.breaker_state() == CIRCUIT_CLOSED
        # 熔断仍可正常服务（没被无谓地开路）
        assert p.breaker.allow() is True

    def test_retryable_predicate_is_consulted_per_failure(self):
        """判定在**每一次**失败上生效：可重试的前几次照样重试。"""
        calls = {"n": 0}

        def fn():
            calls["n"] += 1
            raise adapters.HTTPStatusError(500, "u")

        res = RetryPolicy(max_retries=2).call(
            fn, sleep=lambda _s: None, retryable=lambda _e: True)
        assert calls["n"] == 3 and res.attempts == 3 and res.non_retryable is False

    def test_default_none_keeps_legacy_behaviour(self):
        """不传 retryable 时，任何异常都可重试——旧调用点语义不得改变。"""
        def fn():
            raise adapters.HTTPStatusError(404, "u")

        res = RetryPolicy(max_retries=2).call(fn, sleep=lambda _s: None)
        assert res.attempts == 3 and res.non_retryable is False
        assert res.ok is False


class TestNotFoundTerminalState:
    """404 → `not_found` 终态：不重试、不降级、不缓存、不进熔断。"""

    def test_404_is_single_shot_not_found(self, monkeypatch):
        a, _ = _order_adapter(monkeypatch, policy=RetryPolicy(max_retries=3))
        counters = _install_http_status(monkeypatch, 404)      # 覆盖为带状态码的桩

        r = a.call(order_id="MISSING1")
        assert r["not_found"] is True
        assert r["degraded"] is False, "查不到不是故障，不能混进降级"
        assert r["attempts"] == 1
        assert counters["n"] == 1, "确定性失败不得重试"
        assert "404" in r["error"]

    def test_404_not_counted_as_failure(self, monkeypatch):
        a, _ = _order_adapter(monkeypatch, policy=RetryPolicy(max_retries=3))
        _install_http_status(monkeypatch, 404)

        a.call(order_id="MISSING1")
        assert a.stats["not_found"] == 1
        assert a.stats["degraded"] == 0 and a.stats["failures"] == 0

    def test_404_does_not_trip_breaker(self, monkeypatch):
        p = RetryPolicy(max_retries=1, circuit_breaker=True, cb_threshold=1, cb_cooldown_s=60)
        a, _ = _order_adapter(monkeypatch, policy=p)
        _install_http_status(monkeypatch, 404)

        a.call(order_id="MISSING1")
        assert p.breaker_state() == CIRCUIT_CLOSED

    def test_401_is_non_retryable_but_still_degraded(self, monkeypatch):
        """鉴权失败：不重试、不计熔断，但对用户仍是「实时数据不可用」。"""
        p = RetryPolicy(max_retries=3, circuit_breaker=True, cb_threshold=1, cb_cooldown_s=60)
        a, _ = _order_adapter(monkeypatch, policy=p)
        counters = _install_http_status(monkeypatch, 401)

        r = a.call(order_id="A1")
        assert r["not_found"] is False, "401 不是「没有这条记录」"
        assert r["degraded"] is True and r["non_retryable"] is True
        assert r["attempts"] == 1 and counters["n"] == 1
        assert p.breaker_state() == CIRCUIT_CLOSED, "配置错误不得开路熔断"

    def test_429_is_still_retried(self, monkeypatch):
        """限流是瞬时故障：退避后仍要重试。"""
        a, _ = _order_adapter(monkeypatch, policy=RetryPolicy(max_retries=2))
        counters = _install_http_status(monkeypatch, 429)

        r = a.call(order_id="A1")
        assert r["attempts"] == 3 and counters["n"] == 3
        assert r["degraded"] is True and r["non_retryable"] is False

    def test_inventory_404_is_not_found_too(self, monkeypatch):
        counters = _install_http_status(monkeypatch, 404)
        a = adapters.InventoryAdapter(base_url="http://inv.test", path_tpl="/stock/{id}",
                                      retry_policy=RetryPolicy(max_retries=2),
                                      sleep=lambda _s: None)
        a.mock = False

        r = a.call(sku="NOPE")
        assert r["not_found"] is True and r["sku"] == "NOPE" and counters["n"] == 1

    def test_probe_treats_404_as_ok(self, monkeypatch):
        """自检验的是**配置**：404 证明 URL + 鉴权都通，只是测试 ID 不存在。"""
        a, _ = _order_adapter(monkeypatch, policy=RetryPolicy(max_retries=2))
        _install_http_status(monkeypatch, 404)

        rep = a.probe("TEST1")
        assert rep["ok"] is True and rep["not_found"] is True
        assert "不在" in rep["note"] or "不存在" in rep["note"]


class TestNotFoundCardConsumption:
    """`not_found` 卡在归一化 / 合成层同样要安全且语义正确。"""

    def test_normalize_live_marks_not_found_card(self):
        cards = adapters.normalize_live([
            {"order_id": "MISSING1", "not_found": True, "adapter": "order_status",
             "error": "HTTPStatusError: HTTP 404 Not Found", "attempts": 1},
        ])
        assert len(cards) == 1
        assert cards[0]["type"] == "not_found"
        assert cards[0]["order_id"] == "MISSING1"

    def test_not_found_not_rendered_as_empty_order_card(self):
        """回归：not_found 响应带着 order_id，若不先拦会被判成「订单卡片但状态为空」。"""
        cards = adapters.normalize_live([{"order_id": "MISSING1", "not_found": True,
                                          "adapter": "order_status"}])
        assert cards[0]["type"] != "order"

    def test_not_found_beats_degraded_when_both_flags(self):
        """防御：两个标记同时出现时也不得退化成「空订单卡」（正常路径下互斥）。"""
        cards = adapters.normalize_live([{"order_id": "X", "degraded": True,
                                          "not_found": True, "adapter": "order_status"}])
        assert len(cards) == 1
        assert cards[0]["type"] in ("degraded", "not_found")

    def test_live_line_distinguishes_not_found_from_degraded(self):
        from kb_mcp_server.synthesis import _live_line

        line = _live_line({"type": "not_found", "adapter": "order_status",
                           "order_id": "MISSING1"})
        assert "未查询到" in line and "MISSING1" in line
        assert "非故障" in line, "必须与「后端不可用」区分开"

        degraded = _live_line({"type": "degraded", "adapter": "order_status",
                               "non_retryable": True, "error": "HTTP 401"})
        assert "暂不可用" in degraded and "拒绝" in degraded

    def test_adapter_status_exposes_not_found_count(self):
        st = {x["name"]: x for x in adapters.adapter_status()}
        assert "not_found_calls" in st["order_status"]


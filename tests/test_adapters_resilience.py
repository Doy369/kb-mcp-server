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
"""

from __future__ import annotations

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

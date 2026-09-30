"""外部后端 API 适配器框架（P4 · 生产就绪版）。

设计目标：填了 URL + 字段路径就能真用，无需改代码。

- APIAdapter 基类：统一鉴权（Bearer / 自定义请求头 / 查询参数）、单次尝试超时、TTL 缓存，
  以及 **P0-2 韧性三件套**——有限重试 + 指数退避、熔断（连续失败开路、冷却后半开探测）、
  **降级**（重试耗尽不抛异常，返回带 `degraded` 标记的结构化结果，主链路不崩）。
- 具体适配器：订单状态、库存。每个适配器通过环境变量配置：
  * 基址（KB_ORDER_API_URL / KB_INVENTORY_API_URL）
  * 路径模板（KB_ORDER_PATH_TPL，默认 /orders/{id}）
  * 响应字段路径（JSON 点路径，支持数组索引如 data.items[0].id）
  * 鉴权方式、超时、缓存时长、重试次数与退避、熔断开关与阈值
- 响应字段映射用 Pydantic 校验，缺失字段不报错、标记为 incomplete。
- 默认 mock 模式：未配置真实 endpoint 或 KB_API_MOCK=1 时返回样例，离线即可演示；
  配置 URL 且 KB_API_MOCK=0 即走真实 HTTP。
- 自检：python check_adapters.py 探测已配置的真实 endpoint，打印解析结果，验证 .env 是否接对。

配置（.env）：见 .env.example 的「外部 API（P4 真实集成）」段。
"""

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from abc import ABC, abstractmethod
from typing import Any

from pydantic import BaseModel

from kb_mcp_server.config import get_cfg
from kb_mcp_server.extensions import CIRCUIT_CLOSED, RetryPolicy


# --------------------------------------------------------------------------- #
# 响应校验模型（Pydantic）：规范适配器输出的结构，缺失字段不抛错、标记 incomplete
#
# degraded / error / attempts 是 P0-2 降级语义的载体：实时后端不可用时，
# 这里返回 degraded=True 而非抛异常，UI 与合成层据此提示「实时数据暂不可用」。
# not_found 是另一种**非错误**终态：后端明确回答「没有这条记录」（HTTP 404）。
# 把它归进 degraded 是错的——用户问「订单 SO999 到哪了」，正确回答是「查不到该单号」，
# 而不是「实时数据暂不可用」。两者混淆会让客服去排查一个根本没坏的后端。
# --------------------------------------------------------------------------- #
class OrderStatusResponse(BaseModel):
    order_id: str | None = None
    status: str | None = None
    carrier: str = ""
    eta: str = ""
    mock: bool = False
    degraded: bool = False
    not_found: bool = False
    error: str = ""
    attempts: int = 0
    raw: dict = {}


class InventoryResponse(BaseModel):
    sku: str | None = None
    stock: int | None = None
    warehouse: str = ""
    mock: bool = False
    degraded: bool = False
    not_found: bool = False
    error: str = ""
    attempts: int = 0
    raw: dict = {}


# --------------------------------------------------------------------------- #
# 工具函数
# --------------------------------------------------------------------------- #
def _get_path(obj: Any, path: str) -> Any:
    """按点路径取值，支持数组索引 items[0].id。取不到返回 None。"""
    if not path:
        return None
    cur: Any = obj
    for part in path.split("."):
        if cur is None:
            return None
        if "[" in part:
            name, idx = part[:-1].split("[")
            cur = cur.get(name) if isinstance(cur, dict) else None
            if isinstance(cur, list):
                try:
                    cur = cur[int(idx)]
                except (ValueError, IndexError):
                    return None
            else:
                return None
        else:
            cur = cur.get(part) if isinstance(cur, dict) else None
    return cur


def _enum_paths(obj, prefix="", max_depth=6):
    """枚举 JSON 对象所有叶子节点路径,数组取首个元素继续。返回路径字符串列表(如
    logistics.company、data.items[0].status),与 _get_path 的取数规则一致,供前端做下拉映射。"""
    out = []
    if max_depth <= 0 or obj is None:
        return out
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{prefix}.{k}" if prefix else k
            if isinstance(v, (dict, list)):
                out.extend(_enum_paths(v, p, max_depth - 1))
            else:
                out.append(p)
    elif isinstance(obj, list):
        if obj:
            out.extend(_enum_paths(obj[0], f"{prefix}[0]" if prefix else "[0]", max_depth - 1))
    else:
        if prefix:
            out.append(prefix)
    return out


class HTTPStatusError(Exception):
    """带 **HTTP 状态码** 的失败。

    为什么不能像以前那样让 `urllib.error.HTTPError` 直接冒上去（它本来也带 code）：
    调用方需要按状态码做**策略决策**（重试 or 不重试、降级 or 查不到），而
    `HTTPError` 是 `URLError` 的子类、又混着 JSON 解析后的响应体，语义不好读。
    这里收敛成一个显式类型，把 `status` 摆在面上——`RetryPolicy` 通过
    `getattr(exc, "status", None)` 判定可重试性，不需要 import urllib。
    """

    def __init__(self, status: int, url: str, detail: str = "") -> None:
        super().__init__(f"HTTP {status}" + (f" {detail}" if detail else ""))
        self.status = status
        self.url = url
        self.detail = detail


# 4xx 里**确实值得重试**的两个：
#   408 Request Timeout —— 服务端自己超时，属瞬时故障；
#   429 Too Many Requests —— 限流，退避后重试是对的（真实生产可再读 Retry-After）。
RETRYABLE_4XX = frozenset({408, 429})


def is_retryable(exc: BaseException) -> bool:
    """这次失败值得重试吗？

    4xx（除 408/429）是**确定性失败**：单号不存在、鉴权失败、字段被拒——
    重试只是把同一个结果重复 N 次，白白拖长一次问答并放大下游压力。
    其余（5xx / 连接被拒 / 超时 / DNS 失败）都视为瞬时故障，照旧重试。
    """
    status = getattr(exc, "status", None)
    if isinstance(status, int) and 400 <= status < 500:
        return status in RETRYABLE_4XX
    return True


def _http_get_json(
    url: str,
    api_key: str | None,
    timeout: int,
    scheme: str = "bearer",
    auth_header: str = "Authorization",
    auth_query: str = "",
) -> dict:
    """发起带鉴权的 GET，返回解析后的 JSON。

    失败一律**原样抛出**，由调用方（RetryPolicy / _fetch）决定重试与降级；
    但 HTTP 状态类失败统一转成 `HTTPStatusError`，好让上层能按状态码决策。
    """
    req = urllib.request.Request(url)
    if api_key:
        if scheme == "bearer":
            req.add_header("Authorization", f"Bearer {api_key}")
        elif scheme == "header":
            req.add_header(auth_header, api_key)
        elif scheme == "query":
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}{auth_query}={urllib.parse.quote(api_key, safe='')}"
            req = urllib.request.Request(url)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:  # noqa: PERF203 —— 只转状态类失败，其余透传
        raise HTTPStatusError(e.code, url, str(e.reason or "")) from e


# --------------------------------------------------------------------------- #
# 适配器基类
# --------------------------------------------------------------------------- #
class APIAdapter(ABC):
    """外部后端 API 适配器基类：统一鉴权 / 超时 / 重试 / 缓存。"""

    name: str = "base"
    id_param: str = "id"  # call() 接收的主键参数名（order_id / sku）

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        path_tpl: str = "/{id}",
        timeout: int = 5,
        ttl: int = 30,
        scheme: str = "bearer",
        auth_header: str = "Authorization",
        auth_query: str = "",
        retry_policy: RetryPolicy | None = None,
        sleep=None,
    ):
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key
        self.path_tpl = path_tpl
        self.timeout = timeout
        self.ttl = ttl
        self.scheme = scheme
        self.auth_header = auth_header
        self.auth_query = auth_query
        self.mock = True
        # P0-2：每个适配器**独立**持有策略与熔断器——订单 API 挂掉不应连累库存查询
        self.policy = retry_policy or RetryPolicy(timeout_s=float(timeout))
        self._sleep = sleep                     # 可注入（测试免真等待），默认 time.sleep
        self._cache: dict[str, tuple[float, Any]] = {}
        self.stats = {"calls": 0, "retries": 0, "failures": 0,
                      "degraded": 0, "short_circuited": 0, "not_found": 0}

    def _url(self, key: str) -> str:
        return self.base_url + self.path_tpl.format(id=key)

    def _fetch(self, key: str) -> tuple[dict | None, dict]:
        """取数：返回 `(数据, 元信息)`。**任何失败都不抛异常**，改为降级/查不到返回。

        元信息：{"degraded", "not_found", "cached", "attempts", "error",
                "non_retryable", "short_circuited", "circuit"}。

        注意两点：**失败不写缓存**（否则一次网络抖动会被 TTL 放大成持续 30s 的错误，
        且掩盖熔断的快速失败效果）；**404 与降级是两回事**（见下）。
        """
        now = time.time()
        hit = self._cache.get(key)
        if hit and now - hit[0] < self.ttl:
            return hit[1], {"degraded": False, "cached": True, "attempts": 0,
                            "circuit": self.policy.breaker_state()}

        timeout = max(1, int(round(self.policy.timeout_s)))
        attempt = self.policy.call(
            lambda: _http_get_json(
                self._url(key), self.api_key, timeout,
                self.scheme, self.auth_header, self.auth_query,
            ),
            sleep=self._sleep,
            retryable=is_retryable,
        )
        self.stats["calls"] += 1
        self.stats["retries"] += max(0, attempt.attempts - 1)

        if attempt.ok:
            self._cache[key] = (now, attempt.value)
            return attempt.value, {"degraded": False, "cached": False,
                                   "attempts": attempt.attempts,
                                   "circuit": self.policy.breaker_state()}

        # 404 是**确定性答案**而不是故障：后端明确回答「没有这条记录」。
        # 必须是独立的终态，不能混进 degraded —— 否则「单号查不到」会被渲染成
        # 「实时数据暂不可用」，让客服去排查一个根本没坏的后端。
        status = getattr(attempt.error, "status", None)
        if status == 404:
            self.stats["not_found"] += 1
            return None, {
                "not_found": True, "degraded": False, "cached": False,
                "status": status, "attempts": attempt.attempts,
                "error": attempt.error_text,
                "circuit": self.policy.breaker_state(),
            }

        self.stats["failures"] += 1
        self.stats["degraded"] += 1
        if attempt.short_circuited:
            self.stats["short_circuited"] += 1
        return None, {
            "degraded": True, "cached": False, "attempts": attempt.attempts,
            "error": attempt.error_text, "short_circuited": attempt.short_circuited,
            "non_retryable": attempt.non_retryable,
            "circuit": self.policy.breaker_state(),
        }

    def not_found_result(self, key: str | None, meta: dict) -> dict:
        """把「查不到」落成统一附加字段（子类在自己的 `_not_found` 里复用）。"""
        return {
            "not_found": True,
            "error": meta.get("error", ""),
            "attempts": int(meta.get("attempts", 0)),
            "circuit": meta.get("circuit", CIRCUIT_CLOSED),
            "adapter": self.name,
        }

    def degraded_result(self, key: str | None, meta: dict) -> dict:
        """把降级元信息落成统一的附加字段（子类在自己的 _degraded 里复用）。"""
        return {
            "degraded": True,
            "error": meta.get("error", ""),
            "attempts": int(meta.get("attempts", 0)),
            "short_circuited": bool(meta.get("short_circuited")),
            "non_retryable": bool(meta.get("non_retryable")),
            "circuit": meta.get("circuit", CIRCUIT_CLOSED),
            "adapter": self.name,
        }

    def enabled(self) -> bool:
        return bool(self.base_url) and not self.mock

    @abstractmethod
    def call(self, **params: Any) -> dict: ...

    @abstractmethod
    def _mock(self, key: str | None) -> dict: ...

    def probe(self, test_key: str) -> dict:
        """自检：尝试真实调用（若启用），返回解析结果或错误。"""
        if not self.enabled():
            return {"adapter": self.name, "mode": "mock", "live": False,
                    "note": "未配置 URL 或 KB_API_MOCK=1，处于模拟模式"}
        try:
            r = self.call(**{self.id_param: test_key})
            # P0-2：降级不抛异常，所以这里必须显式判定——否则「取数失败」会被
            # 误报成「自检通过」，用户按这个结论去排查只会更迷惑。
            if r.get("degraded"):
                return {"adapter": self.name, "mode": "live", "live": True, "ok": False,
                        "url": self._url(test_key), "error": r.get("error", ""),
                        "attempts": r.get("attempts", 0),
                        "non_retryable": bool(r.get("non_retryable")),
                        "circuit": r.get("circuit", CIRCUIT_CLOSED),
                        "short_circuited": bool(r.get("short_circuited"))}
            # 404 同样不抛异常，但它**证明了** URL + 鉴权是通的（服务端读懂了请求并
            # 明确回答「没这条记录」）。自检的目的是验配置，所以判 ok=True 并说明
            # 测试 ID 不存在——把「测试单号不存在」报成「配置有问题」会让人白折腾。
            if r.get("not_found"):
                return {"adapter": self.name, "mode": "live", "live": True, "ok": True,
                        "url": self._url(test_key), "not_found": True,
                        "note": f"endpoint 可达且鉴权通过，但测试 ID {test_key!r} 在该系统里"
                                f"不存在；换成真实 ID 即可看到字段映射结果",
                        "error": r.get("error", ""),
                        "circuit": r.get("circuit", CIRCUIT_CLOSED)}
            parsed = {k: r.get(k) for k in ("status", "stock", "carrier", "eta", "warehouse")}
            raw = r.get("raw", {}) or {}
            return {"adapter": self.name, "mode": "live", "live": True, "ok": True,
                    "url": self._url(test_key), "parsed": parsed,
                    "raw_preview": raw, "fields": _enum_paths(raw),
                    "circuit": r.get("circuit", CIRCUIT_CLOSED),
                    "incomplete": all(v in (None, "") for v in parsed.values())}
        except Exception as e:  # noqa: BLE001
            return {"adapter": self.name, "mode": "live", "live": True, "ok": False,
                    "url": self._url(test_key), "error": f"{type(e).__name__}: {e}"}


# --------------------------------------------------------------------------- #
# 具体适配器
# --------------------------------------------------------------------------- #
class OrderStatusAdapter(APIAdapter):
    name = "order_status"
    id_param = "order_id"

    def __init__(self, *a, field_status: str = "status", field_carrier: str = "carrier",
                 field_eta: str = "eta", **kw):
        super().__init__(*a, **kw)
        self.f_status = field_status
        self.f_carrier = field_carrier
        self.f_eta = field_eta

    def call(self, order_id: str | None = None, **_kw) -> dict:
        if not self.enabled():
            return self._mock(order_id)
        data, meta = self._fetch(order_id or "?")
        if meta.get("not_found"):
            return self._not_found(order_id, meta)
        if meta.get("degraded"):
            return self._degraded(order_id, meta)
        return OrderStatusResponse(
            order_id=order_id, status=_get_path(data, self.f_status),
            carrier=_get_path(data, self.f_carrier) or "",
            eta=_get_path(data, self.f_eta) or "", raw=data,
        ).model_dump()

    def _not_found(self, order_id, meta: dict) -> dict:
        return {**OrderStatusResponse(order_id=order_id).model_dump(),
                **self.not_found_result(order_id, meta)}

    def _degraded(self, order_id, meta: dict) -> dict:
        return {**OrderStatusResponse(order_id=order_id).model_dump(),
                **self.degraded_result(order_id, meta)}

    def _mock(self, order_id):
        return OrderStatusResponse(
            order_id=order_id, status="已发货", carrier="顺丰", eta="2026-08-29",
            mock=True, raw={"order_id": order_id, "status": "已发货",
                            "carrier": "顺丰", "eta": "2026-08-29"},
        ).model_dump()


class InventoryAdapter(APIAdapter):
    name = "inventory"
    id_param = "sku"

    def __init__(self, *a, field_stock: str = "stock", field_warehouse: str = "warehouse", **kw):
        super().__init__(*a, **kw)
        self.f_stock = field_stock
        self.f_warehouse = field_warehouse

    def call(self, sku: str | None = None, **_kw) -> dict:
        if not self.enabled():
            return self._mock(sku)
        data, meta = self._fetch(sku or "?")
        if meta.get("not_found"):
            return self._not_found(sku, meta)
        if meta.get("degraded"):
            return self._degraded(sku, meta)
        raw_stock = _get_path(data, self.f_stock)
        return InventoryResponse(
            sku=sku, stock=int(raw_stock) if isinstance(raw_stock, (int, float)) else None,
            warehouse=_get_path(data, self.f_warehouse) or "", raw=data,
        ).model_dump()

    def _not_found(self, sku, meta: dict) -> dict:
        return {**InventoryResponse(sku=sku).model_dump(),
                **self.not_found_result(sku, meta)}

    def _degraded(self, sku, meta: dict) -> dict:
        return {**InventoryResponse(sku=sku).model_dump(),
                **self.degraded_result(sku, meta)}

    def _mock(self, sku):
        return InventoryResponse(
            sku=sku, stock=42, warehouse="华东仓", mock=True,
            raw={"sku": sku, "stock": 42, "warehouse": "华东仓"},
        ).model_dump()


# --------------------------------------------------------------------------- #
# 注册表
# --------------------------------------------------------------------------- #
_REGISTRY: dict[str, APIAdapter] = {}


def register(adapter: APIAdapter) -> None:
    _REGISTRY[adapter.name] = adapter


def get_adapter(name: str) -> APIAdapter | None:
    return _REGISTRY.get(name)


def all_adapters() -> list[APIAdapter]:
    return list(_REGISTRY.values())


def _safe_int(raw, default: int, minimum: int | None = None) -> int:
    """把配置值安全解析为 int。

    运行时配置可被页面写成任意字符串（早期版本写入无校验），直接 int() 会抛
    ValueError 让整个 /api/config 乃至服务 500。这里非法值一律回落到 default，
    保证「配置坏了也能起来」，真正的拦截交给写入侧校验。
    """
    try:
        v = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    if minimum is not None and v < minimum:
        return default
    return v


def _safe_float(raw, default: float, minimum: float | None = None) -> float:
    """把配置值安全解析为 float（与 _safe_int 同理：非法值回落默认，不让服务崩）。"""
    try:
        v = float(str(raw).strip())
    except (TypeError, ValueError):
        return default
    if minimum is not None and v < minimum:
        return default
    return v


def _policy_from_cfg(timeout: int) -> RetryPolicy:
    """按运行时配置构造一份**新的**重试 / 熔断策略。

    每个适配器独占一份：熔断是**按后端**的状态，订单 API 挂掉不该让库存查询
    也跟着快速失败。
    """
    cb_on = str(get_cfg("KB_API_CIRCUIT_BREAKER", "1")).lower() in ("1", "true", "yes")
    return RetryPolicy(
        max_retries=_safe_int(get_cfg("KB_API_MAX_RETRIES", "2"), 2, minimum=0),
        timeout_s=float(timeout),
        backoff_s=_safe_float(get_cfg("KB_API_RETRY_BACKOFF", "0.5"), 0.5, minimum=0.0),
        circuit_breaker=cb_on,
        cb_threshold=_safe_int(get_cfg("KB_API_CIRCUIT_THRESHOLD", "5"), 5, minimum=1),
        cb_cooldown_s=_safe_float(get_cfg("KB_API_CIRCUIT_COOLDOWN", "30"), 30.0, minimum=1.0),
    )


def build_registry() -> dict[str, APIAdapter]:
    """按运行时配置（页面写入，优先于环境变量）构建适配器注册表。"""
    mock = str(get_cfg("KB_API_MOCK", "1")).lower() in ("1", "true", "yes")
    api_key = get_cfg("KB_API_KEY", "")
    scheme = get_cfg("KB_API_AUTH_SCHEME", "bearer").lower()
    auth_header = get_cfg("KB_API_AUTH_HEADER", "Authorization")
    auth_query = get_cfg("KB_API_AUTH_QUERY", "")
    # 数值项容错解析：配置被写成非法值(如 "abc")时回落到默认，绝不让整个服务崩掉
    timeout = _safe_int(get_cfg("KB_API_TIMEOUT", "5"), 5, minimum=1)
    ttl = _safe_int(get_cfg("KB_API_TTL", "30"), 30, minimum=0)

    order = OrderStatusAdapter(
        base_url=get_cfg("KB_ORDER_API_URL", ""), api_key=api_key,
        path_tpl=get_cfg("KB_ORDER_PATH_TPL", "/orders/{id}"),
        field_status=get_cfg("KB_ORDER_STATUS_PATH", "status"),
        field_carrier=get_cfg("KB_ORDER_CARRIER_PATH", "carrier"),
        field_eta=get_cfg("KB_ORDER_ETA_PATH", "eta"),
        timeout=timeout, ttl=ttl, scheme=scheme, auth_header=auth_header, auth_query=auth_query,
        retry_policy=_policy_from_cfg(timeout),
    )
    inv = InventoryAdapter(
        base_url=get_cfg("KB_INVENTORY_API_URL", ""), api_key=api_key,
        path_tpl=get_cfg("KB_INVENTORY_PATH_TPL", "/inventory/{sku}"),
        field_stock=get_cfg("KB_INVENTORY_STOCK_PATH", "stock"),
        field_warehouse=get_cfg("KB_INVENTORY_WAREHOUSE_PATH", "warehouse"),
        timeout=timeout, ttl=ttl, scheme=scheme, auth_header=auth_header, auth_query=auth_query,
        retry_policy=_policy_from_cfg(timeout),
    )
    for a in (order, inv):
        a.mock = mock or not a.base_url
        register(a)
    return _REGISTRY


def reload_adapters() -> dict[str, APIAdapter]:
    """热重载注册表（页面保存配置后调用，使改动立即生效）。"""
    return build_registry()


def self_check() -> dict:
    """探测已配置的真实 endpoint，打印解析结果。供 check_adapters.py / /api/config/test 使用。"""
    report = {"mock": str(get_cfg("KB_API_MOCK", "1")).lower()
              in ("1", "true", "yes"), "adapters": []}
    test_order = get_cfg("KB_ORDER_TEST_ID", "TEST123")
    test_sku = get_cfg("KB_INVENTORY_TEST_SKU", "SKU-TEST")
    order = get_adapter("order_status")
    inv = get_adapter("inventory")
    if order:
        report["adapters"].append(order.probe(test_order))
    if inv:
        report["adapters"].append(inv.probe(test_sku))
    return report


def adapter_status() -> list[dict]:
    """供 Web /api/status 展示每个适配器的真实/模拟状态与熔断状态。

    熔断是「看不见的故障」：开路后调用全被快速失败，但没有可观测指标时
    只能从「实时数据一直不可用」反推。故这里一并暴露 circuit 与降级计数。
    """
    out = []
    for a in all_adapters():
        out.append({"name": a.name, "mode": "live" if a.enabled() else "mock",
                    "base_url": a.base_url or "", "path_tpl": a.path_tpl,
                    "circuit": a.policy.breaker_state(),
                    "degraded_calls": a.stats.get("degraded", 0),
                    "not_found_calls": a.stats.get("not_found", 0),
                    "retries": a.stats.get("retries", 0)})
    return out


def fetch_live(question: str, order_id: str | None = None, sku: str | None = None) -> list[dict]:
    """按显式参数或问题意图，调用对应适配器取实时数据。"""
    results: list[dict] = []
    if order_id:
        a = get_adapter("order_status")
        if a:
            results.append(a.call(order_id=order_id))
    elif any(k in question for k in ("订单", "物流", "发货", "快递")):
        results.append({"adapter": "order_status", "note": "请提供订单号以查询实时状态"})

    if sku:
        a = get_adapter("inventory")
        if a:
            results.append(a.call(sku=sku))
    elif any(k in question for k in ("库存", "有货", "现货", "sku", "SKU")):
        results.append({"adapter": "inventory", "note": "请提供 SKU 以查询实时库存"})
    return results


def normalize_live(live: list[dict] | None) -> list[dict]:
    """把适配器返回的原始实时数据归一化为前端友好的卡片结构（含 Pydantic 校验标记）。"""
    cards: list[dict] = []
    for l in live or []:
        # P0-2 降级优先判定：降级的订单响应仍带着 order_id，若不先拦会被误判成
        # 「订单卡片但状态为空」，用户看到的是「查不到」而不是「后端挂了」。
        if l.get("degraded"):
            cards.append({
                "type": "degraded", "adapter": l.get("adapter", ""),
                "error": l.get("error", ""), "attempts": int(l.get("attempts", 0)),
                "circuit": l.get("circuit", CIRCUIT_CLOSED),
                "short_circuited": bool(l.get("short_circuited")),
                "non_retryable": bool(l.get("non_retryable")),
                "order_id": l.get("order_id"), "sku": l.get("sku"),
            })
            continue
        # 「查不到」是**第三种终态**：后端正常、只是没有这条记录。它同样带着
        # order_id/sku，所以必须排在订单/库存分支之前，否则会被渲染成
        # 「订单卡片但状态为空」。也绝不能并进 degraded——那是「后端出问题」。
        if l.get("not_found"):
            cards.append({
                "type": "not_found", "adapter": l.get("adapter", ""),
                "error": l.get("error", ""), "attempts": int(l.get("attempts", 0)),
                "order_id": l.get("order_id"), "sku": l.get("sku"),
            })
            continue
        if l.get("status") is not None or l.get("order_id"):
            cards.append({
                "type": "order", "order_id": l.get("order_id"), "status": l.get("status"),
                "carrier": l.get("carrier", ""), "eta": l.get("eta", ""),
                "mock": bool(l.get("mock")),
                "incomplete": not l.get("status"),
            })
        elif l.get("stock") is not None or l.get("sku"):
            cards.append({
                "type": "inventory", "sku": l.get("sku"), "stock": l.get("stock"),
                "warehouse": l.get("warehouse", ""), "mock": bool(l.get("mock")),
                "incomplete": l.get("stock") is None,
            })
        elif l.get("note"):
            cards.append({"type": "prompt", "adapter": l.get("adapter"), "note": l.get("note")})
    return cards


# 模块导入即构建注册表
build_registry()

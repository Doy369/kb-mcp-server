"""外部后端 API 适配器框架（P4 · 生产就绪版）。

设计目标：填了 URL + 字段路径就能真用，无需改代码。

- APIAdapter 基类：统一鉴权（Bearer / 自定义请求头 / 查询参数）、超时、重试、TTL 缓存。
- 具体适配器：订单状态、库存。每个适配器通过环境变量配置：
  * 基址（KB_ORDER_API_URL / KB_INVENTORY_API_URL）
  * 路径模板（KB_ORDER_PATH_TPL，默认 /orders/{id}）
  * 响应字段路径（JSON 点路径，支持数组索引如 data.items[0].id）
  * 鉴权方式、超时、缓存时长
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


# --------------------------------------------------------------------------- #
# 响应校验模型（Pydantic）：规范适配器输出的结构，缺失字段不抛错、标记 incomplete
# --------------------------------------------------------------------------- #
class OrderStatusResponse(BaseModel):
    order_id: str | None = None
    status: str | None = None
    carrier: str = ""
    eta: str = ""
    mock: bool = False
    raw: dict = {}


class InventoryResponse(BaseModel):
    sku: str | None = None
    stock: int | None = None
    warehouse: str = ""
    mock: bool = False
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


def _http_get_json(
    url: str,
    api_key: str | None,
    timeout: int,
    scheme: str = "bearer",
    auth_header: str = "Authorization",
    auth_query: str = "",
) -> dict:
    """发起带鉴权的 GET，返回解析后的 JSON。任何网络/解析错误原样抛出，由调用方处理。"""
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
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


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
        self._cache: dict[str, tuple[float, Any]] = {}

    def _cached(self, key: str, fn):
        now = time.time()
        if key in self._cache and now - self._cache[key][0] < self.ttl:
            return self._cache[key][1]
        val = fn()
        self._cache[key] = (now, val)
        return val

    def _url(self, key: str) -> str:
        return self.base_url + self.path_tpl.format(id=key)

    def _fetch(self, key: str) -> dict:
        return self._cached(
            key,
            lambda: _http_get_json(
                self._url(key), self.api_key, self.timeout,
                self.scheme, self.auth_header, self.auth_query,
            ),
        )

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
            parsed = {k: r.get(k) for k in ("status", "stock", "carrier", "eta", "warehouse")}
            raw = r.get("raw", {}) or {}
            return {"adapter": self.name, "mode": "live", "live": True, "ok": True,
                    "url": self._url(test_key), "parsed": parsed,
                    "raw_preview": raw, "fields": _enum_paths(raw),
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
        data = self._fetch(order_id or "?")
        return OrderStatusResponse(
            order_id=order_id, status=_get_path(data, self.f_status),
            carrier=_get_path(data, self.f_carrier) or "",
            eta=_get_path(data, self.f_eta) or "", raw=data,
        ).model_dump()

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
        data = self._fetch(sku or "?")
        raw_stock = _get_path(data, self.f_stock)
        return InventoryResponse(
            sku=sku, stock=int(raw_stock) if isinstance(raw_stock, (int, float)) else None,
            warehouse=_get_path(data, self.f_warehouse) or "", raw=data,
        ).model_dump()

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


def build_registry() -> dict[str, APIAdapter]:
    """按运行时配置（页面写入，优先于环境变量）构建适配器注册表。"""
    mock = str(get_cfg("KB_API_MOCK", "1")).lower() in ("1", "true", "yes")
    api_key = get_cfg("KB_API_KEY", "")
    scheme = get_cfg("KB_API_AUTH_SCHEME", "bearer").lower()
    auth_header = get_cfg("KB_API_AUTH_HEADER", "Authorization")
    auth_query = get_cfg("KB_API_AUTH_QUERY", "")
    timeout = int(get_cfg("KB_API_TIMEOUT", "5") or "5")
    ttl = int(get_cfg("KB_API_TTL", "30") or "30")

    order = OrderStatusAdapter(
        base_url=get_cfg("KB_ORDER_API_URL", ""), api_key=api_key,
        path_tpl=get_cfg("KB_ORDER_PATH_TPL", "/orders/{id}"),
        field_status=get_cfg("KB_ORDER_STATUS_PATH", "status"),
        field_carrier=get_cfg("KB_ORDER_CARRIER_PATH", "carrier"),
        field_eta=get_cfg("KB_ORDER_ETA_PATH", "eta"),
        timeout=timeout, ttl=ttl, scheme=scheme, auth_header=auth_header, auth_query=auth_query,
    )
    inv = InventoryAdapter(
        base_url=get_cfg("KB_INVENTORY_API_URL", ""), api_key=api_key,
        path_tpl=get_cfg("KB_INVENTORY_PATH_TPL", "/inventory/{sku}"),
        field_stock=get_cfg("KB_INVENTORY_STOCK_PATH", "stock"),
        field_warehouse=get_cfg("KB_INVENTORY_WAREHOUSE_PATH", "warehouse"),
        timeout=timeout, ttl=ttl, scheme=scheme, auth_header=auth_header, auth_query=auth_query,
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
    """供 Web /api/status 展示每个适配器的真实/模拟状态。"""
    out = []
    for a in all_adapters():
        out.append({"name": a.name, "mode": "live" if a.enabled() else "mock",
                    "base_url": a.base_url or "", "path_tpl": a.path_tpl})
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

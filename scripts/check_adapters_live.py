"""P0-2 收尾：真实 endpoint 联调（把「只有 mock 与连不上两种实测」补成真实往返）。

为什么需要这一步
----------------
P0-2 的重试 / 退避 / 熔断 / 降级四件套，此前的证据只有两类：
  · **mock 模式**（根本不发 HTTP）；
  · **「后端不可达」**（连接被拒）。
而 `tests/test_adapters_resilience.py` 是**把 HTTP 层整个替换成替身**——
真实状态码、真实鉴权头、真实 socket 超时、真实重试次数，从来没有端到端跑过。
本脚本用 stdlib 起一个**真实本地 HTTP 服务**，让真实适配器用真实 `urllib` 打过去，
逐条钉住这些语义。本地无需任何外部服务，CI 里也照跑。

覆盖（每条都是「真跑才算数」的断言）
------------------------------------
  1) 真实 200 → 字段映射正确（含嵌套点路径 `nested.code`）、`mock=False`
  2) 鉴权三种模式（bearer / header / query）**服务端真的收到了**——不是「代码里写了」
  3) TTL 缓存命中 → 服务端请求数**不增**
  4) 真实 500 → 重试 `max_retries+1` 次（服务端计数精确）→ 降级、不抛异常
  5) 真实慢响应 → socket 超时 → 重试 → 降级
  6) 熔断开路 → 服务端请求数**冻结**、耗时趋近 0（快速失败真的生效）
  7) 真实 404 → **不重试**，判为「查不到」（not_found）而不是「后端挂了」
  8) 真实 401 不重试且**不计入熔断**；真实 429 仍重试（限流是瞬时的）
  9) 端到端：真实 app 子进程 + 真实 `/api/ask` → 答案里出现真实实时数据

第 7/8 条是本次联调**发现并修复**的真问题：旧实现「任何异常都可重试」，
于是「单号不存在」的 404 被重试 3 次、并渲染成「实时数据暂不可用（重试 3 次仍失败）」，
把「这个单号查不到」误导成「后端挂了」，让客服去排查一个根本没坏的后端。

设计要点：**失败必须可诊断**（与 `check_age.py` 同款）
------------------------------------------------------
每个阶段独立 `try/except`（异常不逃逸，转成一条失败断言 + 一行注解 + 截断 traceback）；
`adapters_live_report.json` **无论成败都落盘**；失败项输出 `::error::` GitHub 注解，
聚合计数用 `::notice::`——CI 无日志权限时，注解就是唯一的定位通道。

退出码 0 = 全绿；任何一项不成立即 1。本地：
    python scripts/check_adapters_live.py
"""

from __future__ import annotations

import http.client
import json
import os
import socket
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

# --------------------------------------------------------------------------- #
# 环境隔离：必须在 import 项目模块之前（config 在导入期求值 DATA_DIR 等常量）
# --------------------------------------------------------------------------- #
os.environ["KB_DATA_DIR"] = os.path.join(_HERE, ".adapters_live_runtime")
os.environ["KB_AUDIT_LOG"] = "off"
os.environ["KB_ACTION_LOG"] = "off"

from kb_mcp_server import adapters  # noqa: E402
from kb_mcp_server.extensions import (  # noqa: E402
    CIRCUIT_CLOSED,
    CIRCUIT_OPEN,
    RetryPolicy,
)

CHECKS: list[tuple[str, bool, str]] = []
FACTS: dict = {}


# --------------------------------------------------------------------------- #
# 输出与断言
# --------------------------------------------------------------------------- #
def _oneline(s) -> str:
    """GitHub 注解要求单行且不宜过长，否则整条被丢弃。"""
    return str(s).replace("\r", " ").replace("\n", " ")[:400]


def gha(kind: str, title: str, msg: str) -> None:
    """在工作流里输出注解；本地运行时跳过（避免污染终端输出）。"""
    if os.getenv("GITHUB_ACTIONS"):
        print(f"::{kind} title={_oneline(title)}::{_oneline(msg)}")


def check(name: str, ok: bool, detail: str = "") -> bool:
    CHECKS.append((name, bool(ok), detail))
    print(f"  [{'OK  ' if ok else 'FAIL'}] {name}" + (f" —— {detail}" if detail else ""))
    return bool(ok)


def phase(label: str, fn):
    """执行一个阶段。异常不逃逸，转成一条失败断言 + 一行注解 + 截断的 traceback。"""
    try:
        return fn()
    except Exception as e:  # noqa: BLE001
        check(label, False, f"{type(e).__name__}: {e}")
        gha("error", f"适配器联调失败：{label}", f"{type(e).__name__}: {e}")
        print("      " + traceback.format_exc(limit=4).replace("\n", "\n      "))
        return None


# --------------------------------------------------------------------------- #
# 真实本地 HTTP 后端（可编排行为）
# --------------------------------------------------------------------------- #
OK_BODY = {
    "status": "已发货",
    "carrier": "顺丰",
    "eta": "2026-09-30",
    "nested": {"code": "SHIPPED"},
    "data": {"items": [{"id": "i-1"}]},
}


class _Handler(BaseHTTPRequestHandler):
    """按路径前缀编排行为，并**记录每个请求**（含鉴权头 / 查询串）。

    记录是关键：断言必须基于「服务端实际收到了什么、被打了多少次」，
    而不是基于客户端自认为发了什么——后者正是 HTTP 层被替身接管时测不到的。
    """

    protocol_version = "HTTP/1.1"

    def log_message(self, *a):  # 静音
        pass

    def _send(self, status: int, obj: dict) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass  # 客户端已超时断开（超时场景正常现象）

    def do_GET(self):  # noqa: N802
        srv: _FakeBackend = self.server  # type: ignore[assignment]
        parsed = urllib.parse.urlparse(self.path)
        path, qs = parsed.path, urllib.parse.parse_qs(parsed.query)
        with srv.lock:
            srv.requests.append({
                "path": path, "query": parsed.query, "headers": dict(self.headers),
            })

        # 鉴权校验（仅当本轮要求时）
        if srv.require_key:
            if srv.require_scheme == "bearer":
                ok = self.headers.get("Authorization", "") == f"Bearer {srv.require_key}"
            elif srv.require_scheme == "header":
                ok = self.headers.get(srv.require_header, "") == srv.require_key
            else:
                ok = (qs.get(srv.require_query) or [""])[0] == srv.require_key
            if not ok:
                return self._send(401, {"error": "unauthorized"})

        key = path
        for prefix in ("/orders/", "/inventory/"):
            if path.startswith(prefix):
                key = path[len(prefix):]
                break
        else:
            key = path.lstrip("/")

        if key.startswith("ERR"):
            return self._send(500, {"error": "boom"})
        if key.startswith("MISSING"):
            return self._send(404, {"error": "order not found"})
        if key.startswith("DENIED"):
            return self._send(401, {"error": "unauthorized"})
        if key.startswith("RATE"):
            return self._send(429, {"error": "slow down"})
        if key.startswith("SLOW"):
            time.sleep(srv.slow_s)
            return self._send(200, OK_BODY)
        if key.startswith("FLAKY"):
            with srv.lock:
                srv.flaky_seen[key] = srv.flaky_seen.get(key, 0) + 1
                n = srv.flaky_seen[key]
            if n <= srv.flaky_fails:
                return self._send(500, {"error": "flaky"})
            return self._send(200, OK_BODY)
        return self._send(200, OK_BODY)


class _FakeBackend(ThreadingHTTPServer):
    """真实 socket + 真实 HTTP 协议的本地替身后端（不是 mock 函数）。"""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), _Handler)
        self.lock = threading.Lock()
        self.requests: list[dict] = []
        self.slow_s = 2.5
        self.flaky_seen: dict[str, int] = {}
        self.flaky_fails = 1
        self.require_key = ""
        self.require_scheme = "bearer"
        self.require_header = "X-Api-Key"
        self.require_query = "token"

    @property
    def port(self) -> int:
        return int(self.server_address[1])

    def count(self, path: str) -> int:
        with self.lock:
            return sum(1 for r in self.requests if r["path"] == path)

    def last(self, path: str) -> dict | None:
        with self.lock:
            hits = [r for r in self.requests if r["path"] == path]
            return hits[-1] if hits else None

    def reset(self) -> None:
        with self.lock:
            self.requests.clear()
            self.flaky_seen.clear()

    def handle_error(self, request, client_address) -> None:  # type: ignore[override]
        """静音预期的客户端中断。

        超时场景下客户端读超时后会主动断开 socket，服务端仍在 write/read 就会抛
        ConnectionAbortedError / ConnectionResetError / BrokenPipeError——这是**被测行为本身**
        （超时生效）造成的，属于预期噪音。若照默认实现打印 traceback，
        CI 日志里会出现一堆像失败的堆栈，掩盖真实断言结果。
        """
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionError, BrokenPipeError)):
            return
        super().handle_error(request, client_address)


def _hdr(rec: dict, name: str) -> str:
    """大小写不敏感地取请求头。

    必须这么取：`urllib.request.Request.add_header` 会把头名 `.capitalize()`
    （`X-Api-Key` → `X-api-key`），所以客户端「写了什么」和「服务端收到什么」的
    大小写可能不同。HTTP 头本来就大小写不敏感，断言也不该在意大小写。
    """
    for k, v in (rec.get("headers") or {}).items():
        if k.lower() == name.lower():
            return v
    return ""


def _order(port: int, *, max_retries: int = 2, ttl: int = 30, timeout_s: float = 5.0,
           **kw) -> adapters.OrderStatusAdapter:
    """构造一个「真实模式」订单适配器；退避注入为 no-op（重试**次数**仍真实统计）。"""
    a = adapters.OrderStatusAdapter(
        base_url=f"http://127.0.0.1:{port}", path_tpl="/orders/{id}",
        retry_policy=RetryPolicy(max_retries=max_retries, timeout_s=timeout_s,
                                 backoff_s=0.05),
        sleep=lambda _s: None, **kw,
    )
    a.mock = False
    a.ttl = ttl
    return a


# --------------------------------------------------------------------------- #
# 阶段一：真实 200 / 字段映射 / 鉴权 / 缓存
# --------------------------------------------------------------------------- #
def _stage_happy_path(be: _FakeBackend) -> None:
    a = _order(be.port, field_status="nested.code")
    r = a.call(order_id="OK1")
    check("真实 200：嵌套点路径字段映射", r.get("status") == "SHIPPED",
          f"status={r.get('status')!r}（源 JSON nested.code）")
    check("真实 200：mock=False / degraded=False",
          r.get("mock") is False and r.get("degraded") is False,
          f"mock={r.get('mock')} degraded={r.get('degraded')}")
    check("真实 200：服务端确实收到 1 次请求", be.count("/orders/OK1") == 1,
          f"服务端计数 = {be.count('/orders/OK1')}")

    # TTL 缓存命中：服务端不应再被打
    before = be.count("/orders/OK1")
    r2 = a.call(order_id="OK1")
    check("TTL 缓存命中：不产生新的下游请求",
          be.count("/orders/OK1") == before and r2.get("status") == "SHIPPED",
          f"服务端计数仍为 {be.count('/orders/OK1')}")


def _stage_auth(be: _FakeBackend) -> None:
    """鉴权三模式：断言的是**服务端收到的东西**，而不是客户端代码写了什么。"""
    be.require_key, be.require_scheme = "SECRET-42", "bearer"
    a = _order(be.port, api_key="SECRET-42")
    r = a.call(order_id="AUTHB")
    rec = be.last("/orders/AUTHB") or {}
    check("鉴权 bearer：服务端收到 Authorization: Bearer <key>",
          r.get("degraded") is False
          and _hdr(rec, "Authorization") == "Bearer SECRET-42",
          f"实测收到 {_hdr(rec, 'Authorization')!r}")

    be.require_scheme, be.require_header = "header", "X-Api-Key"
    a = _order(be.port, api_key="SECRET-42", scheme="header", auth_header="X-Api-Key")
    r = a.call(order_id="AUTHH")
    rec = be.last("/orders/AUTHH") or {}
    check("鉴权 header：服务端收到自定义头 X-Api-Key",
          r.get("degraded") is False and _hdr(rec, "X-Api-Key") == "SECRET-42",
          f"实测收到 {_hdr(rec, 'X-Api-Key')!r}")

    be.require_scheme, be.require_query = "query", "token"
    a = _order(be.port, api_key="SECRET-42", scheme="query", auth_query="token")
    r = a.call(order_id="AUTHQ")
    rec = be.last("/orders/AUTHQ") or {}
    check("鉴权 query：apikey 真的出现在查询串里",
          r.get("degraded") is False and "token=SECRET-42" in (rec.get("query") or ""),
          f"实测查询串 {rec.get('query')!r}")

    # 鉴权失败（401）必须被识别为「配置问题」而不是「后端挂了」
    be.require_key = "SECRET-42"
    a = _order(be.port, api_key="WRONG")
    r = a.call(order_id="AUTHQ")
    check("鉴权失败（401）：不重试、error 里带状态码",
          r.get("degraded") is True and r.get("attempts") == 1
          and "401" in str(r.get("error", "")),
          f"attempts={r.get('attempts')} error={r.get('error')!r}")
    be.require_key = ""


# --------------------------------------------------------------------------- #
# 阶段二：真实 5xx 重试 / 慢响应超时 / 熔断
# --------------------------------------------------------------------------- #
def _stage_retry_and_timeout(be: _FakeBackend) -> None:
    be.reset()
    a = _order(be.port, max_retries=2)
    r = a.call(order_id="ERR1")
    check("真实 500：重试 max_retries+1 次后降级（不抛异常）",
          r.get("degraded") is True and r.get("attempts") == 3
          and be.count("/orders/ERR1") == 3,
          f"attempts={r.get('attempts')} 服务端计数={be.count('/orders/ERR1')}")
    check("真实 500：error 里带 HTTP 状态码", "500" in str(r.get("error", "")),
          f"error={r.get('error')!r}")

    be.reset()
    a = _order(be.port, max_retries=1, timeout_s=1.0)
    t0 = time.perf_counter()
    r = a.call(order_id="SLOW1")
    elapsed = time.perf_counter() - t0
    check("真实慢响应：socket 超时触发重试后降级",
          r.get("degraded") is True and r.get("attempts") == 2
          and be.count("/orders/SLOW1") == 2,
          f"attempts={r.get('attempts')} 服务端计数={be.count('/orders/SLOW1')} "
          f"耗时 {elapsed:.2f}s")
    check("真实慢响应：单次超时受 timeout 约束（不应等满服务端 2.5s）",
          elapsed < 2.4, f"实测 {elapsed:.2f}s（服务端 sleep 2.5s，timeout 1s）")
    check("真实慢响应：error 是超时类而不是 HTTP 状态",
          any(k in str(r.get("error", "")) for k in ("Timeout", "timed out")),
          f"error={r.get('error')!r}")


def _stage_circuit(be: _FakeBackend) -> None:
    be.reset()
    p = RetryPolicy(max_retries=0, backoff_s=0.05, circuit_breaker=True,
                    cb_threshold=2, cb_cooldown_s=60)
    a = adapters.OrderStatusAdapter(
        base_url=f"http://127.0.0.1:{be.port}", path_tpl="/orders/{id}",
        retry_policy=p, sleep=lambda _s: None,
    )
    a.mock = False
    a.call(order_id="ERR2")
    a.call(order_id="ERR2")
    check("熔断：连续失败达阈值后开路", p.breaker_state() == CIRCUIT_OPEN,
          f"state={p.breaker_state()}，服务端计数={be.count('/orders/ERR2')}")

    before = be.count("/orders/ERR2")
    t0 = time.perf_counter()
    r = a.call(order_id="ERR2")
    elapsed = time.perf_counter() - t0
    check("熔断开路：下游请求数**冻结**（不再发起调用）",
          be.count("/orders/ERR2") == before,
          f"服务端计数 {before} → {be.count('/orders/ERR2')}")
    check("熔断开路：快速失败（毫秒级，不再等超时）",
          elapsed < 0.2 and r.get("short_circuited") is True,
          f"耗时 {elapsed * 1000:.1f}ms short_circuited={r.get('short_circuited')}")


# --------------------------------------------------------------------------- #
# 阶段三：确定性失败（404/401 不重试、429 仍重试）+ 熔断不被确定性失败误伤
# --------------------------------------------------------------------------- #
def _stage_deterministic(be: _FakeBackend) -> None:
    be.reset()
    a = _order(be.port, max_retries=2)
    r = a.call(order_id="MISSING1")
    check("真实 404：不重试（attempts=1，服务端只被打 1 次）",
          r.get("attempts") == 1 and be.count("/orders/MISSING1") == 1,
          f"attempts={r.get('attempts')} 服务端计数={be.count('/orders/MISSING1')}")
    check("真实 404：判为「查不到」而不是「降级」",
          r.get("not_found") is True and r.get("degraded") is False,
          f"not_found={r.get('not_found')} degraded={r.get('degraded')}")
    cards = adapters.normalize_live([r])
    check("真实 404：归一化为 not_found 卡片（不是 degraded / 空订单卡）",
          len(cards) == 1 and cards[0]["type"] == "not_found",
          f"cards={cards}")

    be.reset()
    a = _order(be.port, max_retries=2)
    r = a.call(order_id="DENIED1")
    check("真实 401：不重试（确定性失败）",
          r.get("attempts") == 1 and be.count("/orders/DENIED1") == 1
          and r.get("non_retryable") is True,
          f"attempts={r.get('attempts')} 服务端计数={be.count('/orders/DENIED1')} "
          f"non_retryable={r.get('non_retryable')}")

    # 确定性失败**不能**把熔断打开：401 是配置问题，不是后端不可用。
    # 否则几次鉴权失败之后所有请求都被短路，真正的原因被彻底掩盖。
    p = RetryPolicy(max_retries=2, backoff_s=0.05, circuit_breaker=True,
                    cb_threshold=1, cb_cooldown_s=60)
    a = adapters.OrderStatusAdapter(
        base_url=f"http://127.0.0.1:{be.port}", path_tpl="/orders/{id}",
        retry_policy=p, sleep=lambda _s: None,
    )
    a.mock = False
    a.call(order_id="DENIED1")
    check("真实 401：不计入熔断（阈值 1 也不开路）",
          p.breaker_state() == CIRCUIT_CLOSED,
          f"state={p.breaker_state()}（应为 closed：配置问题 ≠ 后端不可用）")

    be.reset()
    a = _order(be.port, max_retries=1)
    r = a.call(order_id="RATE1")
    check("真实 429：仍重试（限流是瞬时故障）",
          r.get("attempts") == 2 and be.count("/orders/RATE1") == 2,
          f"attempts={r.get('attempts')} 服务端计数={be.count('/orders/RATE1')}")

    be.reset()
    a = _order(be.port, max_retries=2)
    r = a.call(order_id="FLAKY1")     # 前 1 次 500，之后 200
    check("瞬时故障自愈：重试后拿到真实 200（而非降级）",
          r.get("degraded") is False and r.get("status") == "已发货"
          and be.count("/orders/FLAKY1") == 2,
          f"attempts={r.get('attempts')} 服务端计数={be.count('/orders/FLAKY1')}")


# --------------------------------------------------------------------------- #
# 阶段四：端到端（真实 app 子进程 + 真实 /api/ask）
# --------------------------------------------------------------------------- #
def _ask(port: int, payload: dict, timeout: float = 30.0) -> tuple[int, dict]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        body = json.dumps(payload).encode("utf-8")
        conn.request("POST", "/api/ask", body=body,
                     headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        raw = resp.read().decode("utf-8")
        return resp.status, (json.loads(raw) if raw else {})
    finally:
        conn.close()


def _get(port: int, path: str, timeout: float = 5.0) -> int:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        conn.request("GET", path)
        resp = conn.getresponse()
        resp.read()
        return resp.status
    finally:
        conn.close()


def _stage_e2e(be: _FakeBackend, workdir: str) -> None:
    """真实 app 子进程 + 真实实时后端：证明配置链路（env → 注册表 → /api/ask）是通的。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()

    env = dict(os.environ)
    env.update({
        "KB_DATA_DIR": workdir,
        "KB_MEM_STORE": os.path.join(workdir, "store.json"),
        "KB_GRAPH_STORE": os.path.join(workdir, "graph.json"),
        "KB_STORAGE_BACKEND": "memory",
        "KB_GRAPH_BACKEND": "memory",
        "KB_EMBEDDING_BACKEND": "dev",
        "KB_LLM_ENABLED": "0",
        "KB_AGENT_MODE": "deterministic",
        # ↓ 关键：关掉 mock、指向**真实本地 HTTP 后端**
        "KB_API_MOCK": "0",
        "KB_ORDER_API_URL": f"http://127.0.0.1:{be.port}",
        "KB_ORDER_PATH_TPL": "/orders/{id}",
        "KB_ORDER_STATUS_PATH": "nested.code",
        "KB_INVENTORY_API_URL": f"http://127.0.0.1:{be.port}",
        "KB_INVENTORY_PATH_TPL": "/inventory/{sku}",
        "KB_API_MAX_RETRIES": "1",
        "KB_API_RETRY_BACKOFF": "0.05",
        "KB_API_TIMEOUT": "2",
        "KB_API_TTL": "0",
        "KB_WEB_PORT": str(port),
        "PORT": str(port),
        "KB_NO_BROWSER": "1",
        "BROWSER": "none",
    })
    log = os.path.join(workdir, "app_live.log")
    with open(log, "w", encoding="utf-8", errors="replace") as fh:
        proc = subprocess.Popen([sys.executable, "app.py"], cwd=_HERE, env=env,
                                stdout=fh, stderr=subprocess.STDOUT)
    try:
        deadline = time.time() + 90
        while time.time() < deadline:
            if proc.poll() is not None:
                with open(log, encoding="utf-8", errors="replace") as f:
                    raise RuntimeError(f"app 子进程提前退出（exit={proc.returncode}）\n"
                                       + "".join(f.readlines()[-25:]))
            try:
                if _get(port, "/healthz") == 200:
                    break
            except Exception:
                pass
            time.sleep(0.3)
        else:
            raise RuntimeError("app 子进程 90s 内未就绪")

        be.reset()
        code, obj = _ask(port, {"question": "订单到哪了", "order_id": "OK9", "top_k": 2})
        blob = json.dumps(obj, ensure_ascii=False)
        check("端到端：/api/ask 走真实实时后端并正常返回", code == 200,
              f"HTTP {code}")
        check("端到端：答案里带出真实后端的数据（不是 mock 值）",
              "顺丰" in blob and "SHIPPED" in blob,
              "答案含真实承运商与真实状态码（mock 模式不会有 SHIPPED）")
        check("端到端：服务端确实被真实调用了一次",
              be.count("/orders/OK9") >= 1, f"服务端计数={be.count('/orders/OK9')}")
        FACTS["e2e_server_calls"] = be.count("/orders/OK9")
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()


# --------------------------------------------------------------------------- #
# 报告
# --------------------------------------------------------------------------- #
def _write_report(failed: list[str]) -> None:
    """报告无论成败都要落盘——否则 CI 失败时连「失败在哪一步」都拿不到。"""
    report = {
        "passed": len(CHECKS) - len(failed),
        "total": len(CHECKS),
        "failures": failed,
        "checks": [{"name": n, "ok": ok, "detail": d} for n, ok, d in CHECKS],
        "facts": FACTS,
    }
    rp = os.path.join(_HERE, "adapters_live_report.json")
    try:
        with open(rp, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        print(f"[adapters-live] 报告已写入：{rp}")
    except Exception as e:  # noqa: BLE001
        print(f"[warn] 报告写入失败：{e}", file=sys.stderr)


def main() -> int:
    import tempfile

    print("=" * 68)
    print("P0-2 真实 endpoint 联调（真实本地 HTTP 后端 + 真实 urllib 往返）")
    print("=" * 68)

    be = _FakeBackend()
    threading.Thread(target=be.serve_forever, daemon=True).start()
    print(f"  本地替身后端：http://127.0.0.1:{be.port}（真实 socket / 真实 HTTP 协议）")
    FACTS["fake_backend_port"] = be.port

    workdir = tempfile.mkdtemp(prefix="kb_adapters_live_")
    try:
        phase("阶段一：真实 200 / 字段映射 / 鉴权 / 缓存", lambda: _stage_happy_path(be))
        phase("阶段一b：鉴权三模式（服务端视角）", lambda: _stage_auth(be))
        phase("阶段二：真实 5xx 重试 / 慢响应超时", lambda: _stage_retry_and_timeout(be))
        phase("阶段三：熔断开路与快速失败", lambda: _stage_circuit(be))
        phase("阶段四：确定性失败（404/401/429）与熔断隔离",
              lambda: _stage_deterministic(be))
        phase("阶段五：端到端（真实 app 子进程 + /api/ask）",
              lambda: _stage_e2e(be, workdir))
    except BaseException as e:  # noqa: BLE001  兜底：报告必须产出
        check("整体执行", False, f"{type(e).__name__}: {e}")
        traceback.print_exc()
    finally:
        be.shutdown()
        be.server_close()

    failed = [n for n, ok, _ in CHECKS if not ok]
    _write_report(failed)

    for name, ok, detail in CHECKS:
        if not ok:
            gha("error", f"适配器联调断言失败：{name}", detail)
    gha("notice", "适配器联调结论（真跑凭据）",
        f"共 {len(CHECKS)} 项，失败 {len(failed)} 项；"
        f"真实后端被调用 {FACTS.get('e2e_server_calls') or 0} 次（e2e）")

    print("-" * 68)
    if failed:
        print(f"[FAIL] 适配器联调未通过（{len(failed)}/{len(CHECKS)} 项失败）：{failed}",
              file=sys.stderr)
    else:
        print(f"[OK] 适配器联调全部通过（{len(CHECKS)} 项）："
              "真实 200 / 鉴权三模式 / TTL 缓存 / 5xx 重试 / 超时 / 熔断 / "
              "404 与 401 不重试 / 429 重试 / 端到端")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

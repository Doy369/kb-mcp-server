"""知识库演示控制台（Web 客户端展示 + P5 加固）。

纯标准库 HTTP 服务，复用 kb_mcp_server 的摄取/检索/合成逻辑；
离线可用（memory 后端），切 pgvector 后同一 UI 照用。

P5 加固：
- 可选 Bearer 鉴权（KB_API_TOKEN 非空即启用）
- 按 IP 限流（KB_RATE_LIMIT，每 IP 每分钟请求数，0=不限）
- 结构化访问日志（轨迹 ID + 延迟 + 命中数 + 实时数据数）
- /api/metrics 运行指标；/api/docs 文档列表；/api/delete_doc 删除文档
- 问答走 synthesis 合成层，返回置信度/轨迹/实时数据卡片

运行：python app.py  ->  浏览器打开 http://localhost:8000
"""

import base64
import json
import os
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from kb_mcp_server.adapters import adapter_status, fetch_live, reload_adapters, self_check
from kb_mcp_server.config import get_settings, load_runtime_config, set_cfg
from kb_mcp_server.embeddings import get_embedder
from kb_mcp_server.ingestion import IngestionPipeline
from kb_mcp_server.retrieval import HybridRetriever
from kb_mcp_server.storage import PGVectorStore, get_store
from kb_mcp_server.synthesis import synthesize

settings = get_settings()
_store = get_store()
if isinstance(_store, PGVectorStore):
    _store.connect()
_store.ensure_schema()
_emb = get_embedder()
_retriever = HybridRetriever(_store, _emb)
_ingestor = IngestionPipeline(_store, _emb)

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")

# 前端「接口配置」面板管理的配置项（页面可写，持久化到 runtime_config.json，覆盖环境变量）
API_CONFIG_KEYS = [
    "KB_API_MOCK", "KB_API_KEY", "KB_API_AUTH_SCHEME", "KB_API_AUTH_HEADER", "KB_API_AUTH_QUERY",
    "KB_API_TIMEOUT", "KB_API_TTL",
    "KB_ORDER_API_URL", "KB_ORDER_PATH_TPL", "KB_ORDER_STATUS_PATH",
    "KB_ORDER_CARRIER_PATH", "KB_ORDER_ETA_PATH", "KB_ORDER_TEST_ID",
    "KB_INVENTORY_API_URL", "KB_INVENTORY_PATH_TPL", "KB_INVENTORY_STOCK_PATH",
    "KB_INVENTORY_WAREHOUSE_PATH", "KB_INVENTORY_TEST_SKU",
    "KB_LLM_ENABLED", "KB_LLM_PROVIDER", "KB_LLM_MODEL", "KB_LLM_BASE_URL", "KB_LLM_API_KEY",
]
_KEY_MASK = "***已设置***"
_MASKED_KEYS = ("KB_API_KEY", "KB_LLM_API_KEY")

SAMPLE = {
    "faq_returns": (
        "如何申请退货？在订单签收后 7 天内，于「我的订单」点击申请退货，"
        "填写退货原因并上传凭证，审核通过后寄回商品即可。退货审核一般 1-2 个工作日完成。"
        "退款将原路返回，到账时间取决于支付渠道，通常 3-5 个工作日。"
    ),
    "faq_invoice": (
        "怎么开具发票？订单完成后进入「订单详情 - 申请开票」，选择电子发票或纸质发票，"
        "填写抬头与税号后提交。电子发票实时开具并发送至预留邮箱；纸质发票随下次发货寄出。"
    ),
    "faq_shipping": (
        "物流多久发货？现货商品在付款后 48 小时内出库，偏远地区顺延。可在「我的订单」"
        "查看物流单号与实时轨迹。大客户批量订单按合同约定排期交付。"
    ),
    "faq_account": (
        "账号如何开通子账号？企业管理员在「团队管理 - 成员」邀请同事，按角色分配权限"
        "（客服/运维/管理员）。子账号共享企业工单与知识库，操作留痕可追溯。"
    ),
}

# ---- 可观测：指标 / 限流 / 日志 ----
_metrics_lock = threading.Lock()
_metrics = {
    "requests_total": 0,
    "by_endpoint": {},
    "errors": 0,
    "asks_with_live": 0,
}
_START = time.time()

_rl_lock = threading.Lock()
_rl: dict[str, list] = {}  # ip -> [count, window_start]


def _log_event(trace_id, method, path, status, ms, extra=None):
    rec = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "trace_id": trace_id,
        "method": method,
        "path": path,
        "status": status,
        "ms": ms,
    }
    if extra:
        rec.update(extra)
    line = json.dumps(rec, ensure_ascii=False)
    if settings.log_file:
        try:
            with open(settings.log_file, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            print(line)
    else:
        print("[ACCESS]", line)


def _guard(ip: str, headers, query: dict) -> tuple[bool, int, dict | None]:
    """鉴权 + 限流。返回 (ok, code, error_obj)。"""
    # 鉴权
    if settings.api_token:
        auth = headers.get("Authorization", "")
        token = None
        if auth.lower().startswith("bearer "):
            token = auth[7:].strip()
        elif "token" in query:
            token = query["token"][0]
        if token != settings.api_token:
            return False, 401, {"error": "unauthorized", "hint": "需 Authorization: Bearer <KB_API_TOKEN>"}
    # 限流
    if settings.rate_limit > 0:
        now = time.time()
        with _rl_lock:
            bucket = _rl.get(ip)
            if not bucket or now - bucket[1] >= 60:
                _rl[ip] = [1, now]
            else:
                bucket[0] += 1
                if bucket[0] > settings.rate_limit:
                    return False, 429, {"error": "rate_limited", "retry_after": 60}
    return True, 200, None


def _metrics_inc(path: str, is_error: bool, extra: dict | None):
    with _metrics_lock:
        _metrics["requests_total"] += 1
        ep = path.split("/")[-1] or path
        _metrics["by_endpoint"][ep] = _metrics["by_endpoint"].get(ep, 0) + 1
        if is_error:
            _metrics["errors"] += 1
        if extra:
            if extra.get("asks_with_live"):
                _metrics["asks_with_live"] += 1


class Handler(BaseHTTPRequestHandler):
    def _send_json(self, code: int, obj) -> None:
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_file(self, name: str) -> None:
        path = os.path.join(STATIC_DIR, name)
        with open(path, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        # 禁止缓存，避免浏览器停留在旧版页面（无配置标签）
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    def _dispatch(self, method: str):
        p = urlparse(self.path)
        query = parse_qs(p.query)
        ip = self.client_address[0]
        ok, code, err = _guard(ip, self.headers, query)
        if not ok:
            self._send_json(code, err)
            _log_event("-", method, p.path, code, 0, {"blocked": True})
            _metrics_inc(p.path, True, None)
            return
        start = time.time()
        try:
            if method == "GET":
                code, obj, extra = self._get(p.path)
            else:
                length = int(self.headers.get("Content-Length", 0) or 0)
                raw = self.rfile.read(length) if length else b"{}"
                try:
                    payload = json.loads(raw or b"{}")
                except Exception:
                    payload = {}
                code, obj, extra = self._post(p.path, payload)
        except Exception as e:  # 兜底，避免线程崩
            code, obj, extra = 500, {"error": str(e)}, None
        ms = round((time.time() - start) * 1000, 1)
        self._send_json(code, obj)
        _log_event(obj.get("trace_id", "-") if isinstance(obj, dict) else "-", method, p.path, code, ms, extra)
        _metrics_inc(p.path, code >= 400, extra)

    def _get(self, path: str):
        if path in ("/", "/index.html"):
            return 200, None, None
        if path == "/api/status":
            return 200, {
                "backend": settings.storage_backend,
                "embedding": settings.embedding_backend,
                "llm_enabled": settings.llm_enabled,
                "count": _store.count(),
                "auth_required": bool(settings.api_token),
                "rate_limit": settings.rate_limit,
                "live_apis": adapter_status(),
            }, None
        if path == "/api/docs":
            return 200, _store.list_docs(), None
        if path == "/api/metrics":
            with _metrics_lock:
                return 200, {
                    "uptime_s": round(time.time() - _START, 1),
                    "requests_total": _metrics["requests_total"],
                    "by_endpoint": _metrics["by_endpoint"],
                    "errors": _metrics["errors"],
                    "asks_with_live": _metrics["asks_with_live"],
                }, None
        if path == "/api/config":
            rc = load_runtime_config()
            cfg = {}
            key_set = bool(rc.get("KB_API_KEY"))
            llm_key_set = bool(rc.get("KB_LLM_API_KEY"))
            for k in API_CONFIG_KEYS:
                if k in _MASKED_KEYS:
                    continue  # 密钥不回显明文
                v = rc.get(k)
                cfg[k] = v if v not in (None, "") else os.getenv(k, "")
            cfg["_key_set"] = key_set
            cfg["_llm_key_set"] = llm_key_set
            cfg["_adapters"] = adapter_status()
            return 200, cfg, None
        return 404, {"error": "not found"}, None

    def _post(self, path: str, payload: dict):
        if path == "/api/ingest":
            doc_id = payload.get("doc_id") or f"doc_{_store.count() + 1}"
            text = payload.get("text", "")
            n = _ingestor.ingest_text(doc_id, text) if text else 0
            return 200, {"doc_id": doc_id, "chunks": n, "backend": settings.storage_backend}, None

        if path == "/api/ingest_file":
            filename = payload.get("filename", "")
            content = payload.get("content", "")
            if not filename or not content:
                return 400, {"error": "需提供 filename 与 base64 content"}, None
            doc_id = (payload.get("doc_id")
                      or os.path.splitext(os.path.basename(filename))[0]
                      or f"doc_{_store.count() + 1}")
            try:
                data = base64.b64decode(content)
            except Exception:
                return 400, {"error": "content 需为 base64 编码字节"}, None
            try:
                n = _ingestor.ingest_file(doc_id, filename, data)
            except Exception as e:
                return 400, {"error": f"解析失败({filename})：{e}"}, None
            return 200, {"doc_id": doc_id, "filename": filename,
                         "chunks": n, "backend": settings.storage_backend}, None

        if path == "/api/sample":
            total = 0
            for did, txt in SAMPLE.items():
                total += _ingestor.ingest_text(did, txt)
            return 200, {"ingested": total, "count": _store.count()}, None

        if path == "/api/ingest_folder":
            folder = payload.get("folder", "")
            recursive = bool(payload.get("recursive", True))
            if not folder or not os.path.isdir(folder):
                return 400, {"error": "folder 不存在或无权限", "folder": folder}, None
            exts = (".md", ".markdown", ".txt", ".text", ".docx", ".pdf")
            files = []
            for root, dirs, fnames in os.walk(folder):
                for fn in fnames:
                    if fn.lower().endswith(exts):
                        files.append(os.path.join(root, fn))
                if not recursive:
                    break
            results, skipped = [], []
            for fp in files:
                name = os.path.basename(fp)
                rel = os.path.relpath(fp, folder)
                doc_id = (payload.get("doc_id_prefix", "") + os.path.splitext(rel)[0]).replace(os.sep, "_")
                try:
                    with open(fp, "rb") as fh:
                        data = fh.read()
                    n = _ingestor.ingest_file(doc_id, name, data)
                    results.append({"file": fp, "doc_id": doc_id, "chunks": n})
                except Exception as e:
                    skipped.append({"file": fp, "error": str(e)})
            return 200, {"scanned": len(files), "ingested": len(results),
                         "skipped": skipped, "results": results, "count": _store.count()}, None

        if path == "/api/search":
            hits = _retriever.search(
                payload.get("query", ""),
                top_k=int(payload.get("top_k", 5)),
                mode=payload.get("mode", "hybrid"),
            )
            return 200, hits, {"hits": len(hits)}

        if path == "/api/ask":
            trace_id = uuid.uuid4().hex[:12]
            question = payload.get("question", "")
            top_k = int(payload.get("top_k", 5))
            order_id = payload.get("order_id") or None
            sku = payload.get("sku") or None
            hits = _retriever.search(question, top_k=top_k)
            live = fetch_live(question, order_id=order_id, sku=sku)
            res = synthesize(question, hits, live, trace_id=trace_id)
            res["latency_ms"] = 0  # 由 _dispatch 覆盖日志用，前端自行计时亦可
            extra = {
                "query": question[:50],
                "hits": len(hits),
                "live": len(live),
                "asks_with_live": bool(live),
            }
            return 200, res, extra

        if path == "/api/chat":
            # 对话窗口接口：与 /api/ask 同链路，额外接收多轮 history 供 LLM 合成增强。
            trace_id = uuid.uuid4().hex[:12]
            question = payload.get("question", "")
            top_k = int(payload.get("top_k", 5))
            order_id = payload.get("order_id") or None
            sku = payload.get("sku") or None
            history = payload.get("history") or None
            hits = _retriever.search(question, top_k=top_k)
            live = fetch_live(question, order_id=order_id, sku=sku)
            res = synthesize(question, hits, live, trace_id=trace_id, history=history)
            res["latency_ms"] = 0
            extra = {
                "query": question[:50],
                "hits": len(hits),
                "live": len(live),
                "asks_with_live": bool(live),
            }
            return 200, res, extra

        if path == "/api/delete_doc":
            doc_id = payload.get("doc_id", "")
            n = _store.delete_doc(doc_id) if doc_id else 0
            return 200, {"doc_id": doc_id, "deleted_chunks": n, "count": _store.count()}, None

        if path in ("/api/config", "/api/config/test"):
            # 应用页面提交（含可选测试）：密钥留空则不覆盖；测试前先应用再探测
            applied = {}
            for k in API_CONFIG_KEYS:
                if k not in payload:
                    continue
                v = payload[k]
                if k in _MASKED_KEYS:
                    if not v:  # 留空 = 保留现有密钥
                        continue
                    set_cfg(k, v)
                    applied[k] = True
                    continue
                set_cfg(k, v if v is not None else "")
                applied[k] = True
            reload_adapters()
            if path == "/api/config/test":
                return 200, self_check(), None
            return 200, {"ok": True, "applied": applied, "live_apis": adapter_status()}, None

        return 404, {"error": "not found"}, None

    def do_GET(self):
        p = urlparse(self.path)
        if p.path in ("/", "/index.html"):
            return self._send_file("index.html")
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    port = int(os.getenv("KB_WEB_PORT", "8000"))
    # 端口占用自检：避免重复启动多个实例导致内存存储/摄取互相看不到
    import socket as _sock
    _probe = _sock.socket(_sock.AF_INET, _sock.SOCK_STREAM)
    try:
        _probe.bind(("0.0.0.0", port))
    except OSError:
        print(f"[错误] 端口 {port} 已被占用，已有控制台实例在运行。请先停止旧实例，或换端口(KB_WEB_PORT)。")
        raise SystemExit(1)
    finally:
        _probe.close()
    auth = f" 鉴权=开(KB_API_TOKEN)" if settings.api_token else " 鉴权=关"
    rl = f" 限流={settings.rate_limit}/min" if settings.rate_limit else " 限流=关"
    print(f"知识库演示控制台已启动: http://localhost:{port}  (后端={settings.storage_backend}, 嵌入={settings.embedding_backend}, LLM合成={'开' if settings.llm_enabled else '关'}{auth}{rl})")
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()

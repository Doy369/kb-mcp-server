"""本地 LLM 客户端（OpenAI 兼容接口，如 Ollama / vLLM）。

集中做三件事（此前 synthesis / graph / orchestrator 三处各写了一份 HTTP 调用）：
1. 绕过系统代理——本地 LLM 走 localhost，被 HTTP_PROXY 劫持时连接失败前会白等数秒；
2. 熔断——LLM 不可达时记一次失败，60 秒内所有调用直接返回 None，
   不再每次白等超时（多 agent 链路里一次问答可能触发多次 LLM 调用）；
3. 统一读取 KB_LLM_* 配置（运行时配置优先）。

任何失败都返回 None，调用方自行回退——这是全项目的降级约定。
"""

from __future__ import annotations

import json
import time
import urllib.request

from kb_mcp_server.config import get_cfg

# 熔断到期时间戳（进程内）：0 = 未熔断
_BREAK_UNTIL = 0.0
_BREAK_SECONDS = 60

# 最后一次失败原因（供 /api/status 暴露）。LLM 失败一律静默回退模板，
# 不留痕的话「答案是模板/答非所问」这类问题永远无法从外部定位。
_LAST_ERROR = ""
_LAST_OK_AT = 0.0


def llm_last_error() -> str:
    """返回最近一次 LLM 失败原因；成功过则返回空串。"""
    return _LAST_ERROR


def llm_last_ok_at() -> float:
    """最近一次 LLM 调用成功的时间戳（0 = 本次进程内从未成功）。"""
    return _LAST_OK_AT


def llm_chat(prompt: str, temperature: float = 0.2, max_tokens: int = 400,
             timeout: int = 15) -> str | None:
    """调用 LLM，返回回复文本；禁用 / 熔断中 / 任何失败均返回 None。"""
    global _BREAK_UNTIL, _LAST_ERROR, _LAST_OK_AT
    if time.time() < _BREAK_UNTIL:
        return None
    base = get_cfg("KB_LLM_BASE_URL", "http://localhost:11434/v1").rstrip("/")
    body = json.dumps({
        "model": get_cfg("KB_LLM_MODEL", "qwen2.5:7b"),
        "messages": [{"role": "user", "content": prompt}],
        "temperature": temperature,
        "max_tokens": max_tokens,
    }).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    api_key = get_cfg("KB_LLM_API_KEY", "")
    if api_key:
        headers["Authorization"] = "Bearer " + api_key
    req = urllib.request.Request(base + "/chat/completions", data=body, headers=headers)
    try:
        # 本地 LLM（Ollama/vLLM）绕过系统代理——被 HTTP_PROXY 劫持时会白等数秒；
        # 云端 API（DeepSeek/硅基流动等）则尊重系统代理
        if "://localhost" in base or "://127.0.0.1" in base:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        else:
            opener = urllib.request.build_opener()
        with opener.open(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        txt = (data["choices"][0]["message"]["content"] or "").strip()
        if txt:
            _LAST_OK_AT, _LAST_ERROR = time.time(), ""
        return txt or None
    except Exception as e:  # noqa: BLE001 - 降级约定：任何失败都静默回退
        _BREAK_UNTIL = time.time() + _BREAK_SECONDS
        code = getattr(e, "code", None)
        if code is not None:
            try:
                body = (e.read() or b"").decode("utf-8", "ignore")[:200]
            except Exception:  # noqa: BLE001
                body = ""
            _LAST_ERROR = f"HTTP {code}: {body}" if body else f"HTTP {code}"
        else:
            _LAST_ERROR = f"{type(e).__name__}: {e}"[:300]
        return None


def llm_enabled() -> bool:
    return get_cfg("KB_LLM_ENABLED", "0").lower() in ("1", "true", "yes")


def llm_selfcheck(timeout: int = 20) -> dict:
    """实发一次最小请求做体检，返回可诊断结果（不受 60s 熔断影响，便于改完 key 立刻复验）。

    成功时顺带清掉熔断，让合成链路立即恢复；失败原因写入 _LAST_ERROR 供 /api/status 展示。
    """
    global _BREAK_UNTIL, _LAST_ERROR, _LAST_OK_AT

    base = get_cfg("KB_LLM_BASE_URL", "http://localhost:11434/v1").rstrip("/")
    model = get_cfg("KB_LLM_MODEL", "qwen2.5:7b")
    key = get_cfg("KB_LLM_API_KEY", "")
    local = ("://localhost" in base) or ("://127.0.0.1" in base)

    out = {
        "enabled": llm_enabled(),
        "base_url": base,
        "model": model,
        "api_key_set": bool(key),
        # 只回显首尾几个字符：够判断「填错成占位值」，又不至于泄露整串
        "api_key_hint": (key[:7] + "..." + key[-4:]) if len(key) > 16
                        else ("(疑似占位值，长度异常)" if key else "(未设置)"),
        "ok": False, "http_status": None, "error": "", "reply": "", "latency_ms": 0,
    }
    if not out["enabled"]:
        out["error"] = "KB_LLM_ENABLED=0，未启用合成（答案走模板）"
        return out
    if not key and not local:
        out["error"] = "云端接口但未填 KB_LLM_API_KEY，必然 401"
        return out

    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": "只回复两个字：你好"}],
        "max_tokens": 16,
    }).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = "Bearer " + key
    req = urllib.request.Request(base + "/chat/completions", data=body, headers=headers)

    t0 = time.time()
    try:
        opener = (urllib.request.build_opener(urllib.request.ProxyHandler({}))
                  if local else urllib.request.build_opener())
        with opener.open(req, timeout=timeout) as resp:
            out["http_status"] = resp.status
            data = json.loads(resp.read().decode("utf-8"))
        txt = (data["choices"][0]["message"]["content"] or "").strip()
        out["ok"] = bool(txt)
        out["reply"] = txt[:120]
        if txt:
            _LAST_OK_AT, _LAST_ERROR, _BREAK_UNTIL = time.time(), "", 0.0
    except Exception as e:  # noqa: BLE001 - 体检要的就是失败详情
        code = getattr(e, "code", None)
        if code is not None:
            try:
                raw = (e.read() or b"").decode("utf-8", "ignore")[:300]
            except Exception:  # noqa: BLE001
                raw = ""
            out["http_status"] = code
            out["error"] = f"HTTP {code}: {raw}" if raw else f"HTTP {code}"
        else:
            out["error"] = f"{type(e).__name__}: {e}"[:300]
        _BREAK_UNTIL = time.time() + _BREAK_SECONDS
        _LAST_ERROR = out["error"]
    out["latency_ms"] = int((time.time() - t0) * 1000)
    return out

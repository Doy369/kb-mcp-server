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

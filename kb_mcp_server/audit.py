"""P1-5 审计日志：每次问答追加一条结构化 JSONL 记录（B2B 合规刚需）。

设计：
- 默认开启，写到项目根（exe 模式为 DATA_DIR）下的 kb_audit.jsonl；
  运行时配置 / 环境变量 KB_AUDIT_LOG 可改路径；
  显式设为 off / none / disabled / 0 则**关闭**（评测时用，避免污染真实审计文件）。
- 只追加、不删改；多请求并发时线程安全（锁内追加）。
- 任何异常静默吞掉——审计是旁路，绝不允许拖垮问答主链路。
"""

import json
import os
import threading
import time

from kb_mcp_server.config import DATA_DIR, get_cfg

_lock = threading.Lock()
_OFF = {"off", "none", "disabled", "0", "false", "no"}


def _audit_path() -> str | None:
    """审计日志路径；显式置为 off 类值则返回 None（关闭）。"""
    p = (get_cfg("KB_AUDIT_LOG", "") or "").strip()
    if p.lower() in _OFF:
        return None
    if p:
        return p
    base = DATA_DIR or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, "kb_audit.jsonl")


def audit(entry: dict) -> None:
    """追加一条审计记录（JSONL）。自动补 ts 字段。"""
    try:
        path = _audit_path()
        if not path:
            return
        rec = dict(entry)
        rec.setdefault("ts", time.strftime("%Y-%m-%d %H:%M:%S"))
        with _lock, open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001
        pass

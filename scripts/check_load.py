"""负载测试：在真实 HTTP 栈上打并发，把「能跑」变成「能扛多少」。

为什么需要这一步（ROADMAP P2-10 补）
--------------------------------------
单测与回归评测都是**串行单请求**视角——它们能证明「逻辑对」，但回答不了
「并发上来会不会串味 / 会不会丢数据 / 限流计数器会不会漏」。而这恰好是
`ThreadingHTTPServer` + 全局可变状态（store / 限流桶 / 指标计数器）最典型的失效面：
线程是**真的并发**在跑，任何没上锁的读改写都会在压测下原形毕露。

做法
----
起**真实 app 子进程**（隔离 `KB_DATA_DIR`，memory 存储，dev 嵌入，确定性编排，
mock 实时数据），再用线程池打真实 HTTP 请求。四个场景：

  1) read       只读并发：吞吐 + p50/p95/p99 + 零错误 + 响应字段完整
  2) mixed      混合读写：并发 ingest 与 ask 交错，断言**写入条数与 store 增量一致**
                （丢数据是并发写最隐蔽的失效：不报错，但少了）
  3) ratelimit  限流门：独立实例 `KB_RATE_LIMIT=N`，并发打 M>N 个请求，
                断言**恰好 N 个 200、其余 429**；且 /healthz 不受限流影响
  4) metrics    指标自洽：`/api/metrics` 的 requests_total 不小于实际发送数

与 mock 的关系：这里**完全没有替身**——真实 HTTP、真实检索、真实 store。
外部依赖（LLM / 实时 API）本身已由 mock 开关关掉，不属本次压测目标。

阈值刻意留大余量：CI runner 与开发机不同，压测的目的不是跑分，是**抓退化**。
产出 `load_report.json`；任一断言失败 → 退出码 1。

本地运行：
    python scripts/check_load.py
"""

from __future__ import annotations

import http.client
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor

_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# --------------------------------------------------------------------------- #
# 压测参数与阈值
#
# 阈值的取法：先实测，再留 3–4 倍余量。压测最容易死在「阈值贴着开发机实测值」上——
# 换个 runner 就红，红几次之后就没人看了，等于没有压测。
# --------------------------------------------------------------------------- #
CONCURRENCY = 8          # 并发度（线程数）
READ_TOTAL = 160         # 只读场景总请求数
# 实测（开发机，2026-09-29，两次运行高度一致）：
#   148.1 / 151.8 req/s，p50 29ms，p95 38–58ms，p99 ~515ms
# 阈值取「4–10 倍余量」而不是贴着实测值：CI runner 通常比开发机慢，
# 阈值贴实测值 = 换个 runner 就红 = 红几次之后没人再看。
# 同时仍能抓住真退化：p2-9 那个「首次检索卡 60s」的缺陷会让此时延断言直接爆掉。
P95_MAX_MS = 600.0       # 只读 p95 上限（实测 38–58ms）
P99_MAX_MS = 1500.0      # 只读 p99 上限（实测 ~515ms）
MIN_RPS = 25.0           # 只读吞吐下限（实测 ~150 req/s）

MIX_WRITERS = 40         # 混合场景并发写入数
MIX_READERS = 80         # 混合场景并发读取数

RL_LIMIT = 5             # 限流场景的每分钟配额
RL_TOTAL = 24            # 限流场景并发请求数（须 > RL_LIMIT）
RL_HEALTHZ = 4           # 限流场景同时打的健康检查数（应全部放行）

ASK_BODY = {"question": "如何申请退款", "top_k": 3}


# --------------------------------------------------------------------------- #
# 基础设施
# --------------------------------------------------------------------------- #
def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class Server:
    """真实 app 子进程。日志落文件（不让管道写满阻塞子进程），失败时回读尾部。"""

    def __init__(self, workdir: str, port: int, extra_env: dict):
        self.port = port
        self.log = os.path.join(workdir, f"server_{port}.log")
        env = dict(os.environ)
        env.update({
            "KB_DATA_DIR": workdir,
            "KB_MEM_STORE": os.path.join(workdir, "store.json"),
            "KB_GRAPH_STORE": os.path.join(workdir, "graph.json"),
            "KB_STORAGE_BACKEND": "memory",
            "KB_GRAPH_BACKEND": "memory",
            "KB_GRAPH_ENABLED": "1",
            "KB_EMBEDDING_BACKEND": "dev",
            "KB_LLM_ENABLED": "0",
            "KB_AGENT_MODE": "deterministic",
            "KB_API_MOCK": "1",
            "KB_AUDIT_LOG": "off",
            "KB_WEB_PORT": str(port),
            "PORT": str(port),
            # 绝不自动开浏览器：浏览器会真的加载页面并打出一串 API 请求，
            # 计入限流配额与指标计数 → 压测与限流断言全被污染（实测踩过：
            # 限流场景「放行 5」变成「放行 1」，就是浏览器先吃掉了 4 个配额）。
            "KB_NO_BROWSER": "1",
            "BROWSER": "none",
        })
        env.update(extra_env)
        self._env = env
        self.proc: subprocess.Popen | None = None
        self._fh = None

    def start(self, timeout: float = 90.0) -> None:
        self._fh = open(self.log, "w", encoding="utf-8", errors="replace")
        self.proc = subprocess.Popen(
            [sys.executable, "app.py"],
            cwd=_HERE, env=self._env, stdout=self._fh, stderr=subprocess.STDOUT,
        )
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(
                    f"app 子进程提前退出（exit={self.proc.returncode}）\n{self.tail()}"
                )
            try:
                code, _ = get(self.port, "/healthz", timeout=2.0)
                if code == 200:
                    return
            except Exception:
                pass
            time.sleep(0.3)
        raise RuntimeError(f"app 子进程 {timeout:.0f}s 内未就绪\n{self.tail()}")

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def tail(self, n: int = 30) -> str:
        try:
            with open(self.log, encoding="utf-8", errors="replace") as f:
                return "".join(f.readlines()[-n:])
        except Exception:
            return "(无日志)"

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        if self._fh:
            self._fh.close()


def _request(port: int, method: str, path: str, payload: dict | None = None,
             timeout: float = 30.0) -> tuple[int, dict, float]:
    """单次真实 HTTP 请求。返回 (状态码, 响应体, 耗时 ms)。

    服务端未设 protocol_version（HTTP/1.0，无 keep-alive），所以每次请求都是
    新连接——这本身是压测要暴露的特征，不做连接池掩盖。
    """
    body = json.dumps(payload).encode() if payload is not None else None
    headers = {"Content-Type": "application/json"} if body else {}
    t0 = time.perf_counter()
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        conn.request(method, path, body=body, headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
        code = resp.status
    finally:
        conn.close()
    ms = (time.perf_counter() - t0) * 1000
    try:
        obj = json.loads(raw.decode("utf-8"))
    except Exception:
        obj = {"_raw": raw[:200].decode("utf-8", "replace")}
    return code, obj, ms


def post(port: int, path: str, payload: dict, timeout: float = 30.0):
    return _request(port, "POST", path, payload, timeout)


def get(port: int, path: str, timeout: float = 30.0):
    code, obj, ms = _request(port, "GET", path, None, timeout)
    return code, obj


def _pct(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    xs = sorted(values)
    k = min(len(xs) - 1, max(0, int(round((len(xs) - 1) * q))))
    return xs[k]


# --------------------------------------------------------------------------- #
# 场景
# --------------------------------------------------------------------------- #
def scenario_read(port: int, out: list[str]) -> dict:
    """只读并发：吞吐 + 分位时延 + 零错误 + 响应字段完整。"""
    # 预热：把首次请求才做的惰性初始化（索引构建等）排除在计时之外
    for _ in range(4):
        post(port, "/api/ask", ASK_BODY)

    results: list[tuple[int, dict, float]] = []

    def one(_i: int):
        return post(port, "/api/ask", ASK_BODY)

    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=CONCURRENCY) as ex:
        for r in ex.map(one, range(READ_TOTAL)):
            results.append(r)
    wall = time.perf_counter() - t0

    lat = [ms for _, _, ms in results]
    codes = {}
    bad_shape = []
    for code, obj, _ms in results:
        codes[code] = codes.get(code, 0) + 1
        if code == 200 and not (isinstance(obj, dict) and "answer" in obj and "confidence" in obj):
            bad_shape.append(obj)

    ok = codes.get(200, 0)
    stat = {
        "total": READ_TOTAL,
        "concurrency": CONCURRENCY,
        "ok": ok,
        "codes": codes,
        "rps": round(READ_TOTAL / wall, 2),
        "wall_s": round(wall, 2),
        "p50_ms": round(_pct(lat, 0.50), 1),
        "p95_ms": round(_pct(lat, 0.95), 1),
        "p99_ms": round(_pct(lat, 0.99), 1),
        "max_ms": round(max(lat), 1),
        "bad_shape": len(bad_shape),
    }
    out.append(
        f"  并发 {CONCURRENCY} 发 {READ_TOTAL} 次 /api/ask："
        f"{stat['rps']} req/s，p50 {stat['p50_ms']}ms / p95 {stat['p95_ms']}ms / "
        f"p99 {stat['p99_ms']}ms，状态码 {codes}"
    )
    return stat


def scenario_mixed(port: int, out: list[str]) -> dict:
    """混合读写：并发写 + 并发读交错，断言写入不丢（store 增量 == 上报条数之和）。"""
    code, before = get(port, "/api/status")
    count_before = int(before.get("count", -1))

    ingest_results: list[tuple[int, dict, float]] = []
    ask_results: list[tuple[int, dict, float]] = []

    def writer(i: int):
        doc_id = f"load_{i:03d}"
        text = (f"第{i}号压测工单：客户反馈发货延迟，物流配送异常，"
                f"要求退款退货并升级工单。订单号 SO{i:05d}。")
        return post(port, "/api/ingest", {"doc_id": doc_id, "text": text})

    def reader(i: int):
        return post(port, "/api/ask", {"question": f"工单 {i} 的处理方式", "top_k": 3})

    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=CONCURRENCY) as ex:
        fw = [ex.submit(writer, i) for i in range(MIX_WRITERS)]
        fr = [ex.submit(reader, i) for i in range(MIX_READERS)]
        for f in fw:
            ingest_results.append(f.result())
        for f in fr:
            ask_results.append(f.result())
    wall = time.perf_counter() - t0

    code, after = get(port, "/api/status")
    count_after = int(after.get("count", -1))

    w_ok = [o for c, o, _ in ingest_results if c == 200]
    chunks_reported = sum(int(o.get("chunks", 0)) for o in w_ok)
    ask_ok = sum(1 for c, _, _ in ask_results if c == 200)
    delta = count_after - count_before

    stat = {
        "writers": MIX_WRITERS,
        "writers_ok": len(w_ok),
        "chunks_reported": chunks_reported,
        "readers": MIX_READERS,
        "readers_ok": ask_ok,
        "count_before": count_before,
        "count_after": count_after,
        "count_delta": delta,
        "lost_chunks": chunks_reported - delta,
        "wall_s": round(wall, 2),
    }
    out.append(
        f"  并发 {MIX_WRITERS} 写 + {MIX_READERS} 读：写入成功 {len(w_ok)}，"
        f"上报 {chunks_reported} 片段，store 实际 +{delta}（丢失 {stat['lost_chunks']}），"
        f"读成功 {ask_ok}"
    )
    return stat


def scenario_ratelimit(workdir: str, out: list[str]) -> dict:
    """限流门：并发下计数器是否精确。独立实例（限流是启动期读入的配置）。"""
    port = _free_port()
    srv = Server(workdir, port, {"KB_RATE_LIMIT": str(RL_LIMIT)})
    srv.start()
    try:
        results: list[int] = []

        def one(_i: int):
            return post(port, "/api/ask", ASK_BODY)[0]

        with ThreadPoolExecutor(max_workers=CONCURRENCY) as ex:
            results.extend(ex.map(one, range(RL_TOTAL)))

        # 健康检查不受限流影响（部署平台探活必须永远通）
        hz = [get(port, "/healthz")[0] for _ in range(RL_HEALTHZ)]

        ok = sum(1 for c in results if c == 200)
        rl = sum(1 for c in results if c == 429)
        other = [c for c in results if c not in (200, 429)]
        stat = {
            "limit": RL_LIMIT,
            "sent": RL_TOTAL,
            "concurrency": CONCURRENCY,
            "ok": ok,
            "limited": rl,
            "other": other,
            "healthz_codes": hz,
        }
        out.append(
            f"  限流 {RL_LIMIT}/min：并发 {CONCURRENCY} 发 {RL_TOTAL} 次 → "
            f"放行 {ok}、限流 {rl}、其它 {other or '无'}；/healthz {hz}"
        )
        return stat
    finally:
        srv.stop()


def scenario_metrics(port: int, sent: int, out: list[str]) -> dict:
    """指标自洽：计数器不能比实际收到的请求还少（并发下漏加的典型症状）。"""
    code, m = get(port, "/api/metrics")
    total = int(m.get("requests_total", -1)) if code == 200 else -1
    stat = {"requests_total": total, "sent_at_least": sent, "by_endpoint": m.get("by_endpoint", {})}
    out.append(f"  /api/metrics requests_total={total}（本次至少发出 {sent} 次）")
    return stat


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def main() -> int:
    workdir = tempfile.mkdtemp(prefix="kb_load_")
    port = _free_port()
    srv = Server(workdir, port, {})
    out: list[str] = []
    report: dict = {"concurrency": CONCURRENCY, "thresholds": {
        "p95_max_ms": P95_MAX_MS, "p99_max_ms": P99_MAX_MS, "min_rps": MIN_RPS,
    }}
    failures: list[str] = []

    print("=" * 68)
    print("负载测试（真实 HTTP + 真实检索 + 真实 store，无替身）")
    print("=" * 68)
    try:
        # 播种：与线上演示同源的内置 FAQ 语料
        srv.start()
        code, seeded, _ms = post(port, "/api/sample", {})
        if code != 200:
            raise RuntimeError(f"/api/sample 失败：{code} {seeded}")
        print(f"\n[0] 语料播种：{seeded.get('ingested')} 片段，store 共 {seeded.get('count')} 条\n")

        print("[1] 只读并发")
        r = scenario_read(port, out)
        report["read"] = r
        if r["ok"] != r["total"]:
            failures.append(f"只读场景有 {r['total'] - r['ok']} 个非 200（{r['codes']}）")
        if r["bad_shape"]:
            failures.append(f"只读场景 {r['bad_shape']} 个响应缺 answer/confidence 字段")
        if r["p95_ms"] > P95_MAX_MS:
            failures.append(f"只读 p95 {r['p95_ms']}ms 超过上限 {P95_MAX_MS}ms")
        if r["p99_ms"] > P99_MAX_MS:
            failures.append(f"只读 p99 {r['p99_ms']}ms 超过上限 {P99_MAX_MS}ms")
        if r["rps"] < MIN_RPS:
            failures.append(f"只读吞吐 {r['rps']} req/s 低于下限 {MIN_RPS}")

        print("\n[2] 混合读写（并发写 + 读交错）")
        m = scenario_mixed(port, out)
        report["mixed"] = m
        if m["writers_ok"] != m["writers"]:
            failures.append(f"混合场景写入失败 {m['writers'] - m['writers_ok']} 次")
        if m["readers_ok"] != m["readers"]:
            failures.append(f"混合场景读取失败 {m['readers'] - m['readers_ok']} 次")
        if m["lost_chunks"] != 0:
            failures.append(
                f"并发写入丢数据：上报 {m['chunks_reported']} 片段但 store 只 +{m['count_delta']}"
            )

        print("\n[3] 限流门（独立实例）")
        rl = scenario_ratelimit(workdir, out)
        report["ratelimit"] = rl
        if rl["ok"] != RL_LIMIT:
            failures.append(f"限流放行数 {rl['ok']} ≠ 配额 {RL_LIMIT}（并发下计数器不准）")
        if rl["limited"] != RL_TOTAL - RL_LIMIT:
            failures.append(f"限流拒绝数 {rl['limited']} ≠ 预期 {RL_TOTAL - RL_LIMIT}")
        if any(c != 200 for c in rl["healthz_codes"]):
            failures.append(f"/healthz 被限流误伤：{rl['healthz_codes']}")

        print("\n[4] 指标自洽")
        mt = scenario_metrics(port, READ_TOTAL + MIX_READERS + MIX_WRITERS, out)
        report["metrics"] = mt
        if mt["requests_total"] < READ_TOTAL + MIX_READERS + MIX_WRITERS:
            failures.append(
                f"requests_total {mt['requests_total']} 小于本次发送数"
                f" {READ_TOTAL + MIX_READERS + MIX_WRITERS}（并发下漏加）"
            )

        if not srv.alive():
            failures.append("压测结束后服务进程已退出（疑似线程崩溃拖垮进程）")

    except Exception as e:  # noqa: BLE001
        failures.append(f"压测无法完成：{type(e).__name__}: {e}")
        out.append("")
        out.append("---- 服务端日志尾部 ----")
        out.append(srv.tail())
    finally:
        srv.stop()
        shutil.rmtree(workdir, ignore_errors=True)

    report["pass"] = not failures
    report["failures"] = failures
    # 压测顺手记录的一个既有特征（不是本次要改的缺陷，但不该被忽略）：
    # app.py 没设 protocol_version → BaseHTTPRequestHandler 默认 HTTP/1.0，
    # 即**每请求新建 TCP 连接、无 keep-alive**。并发吞吐会被连接建立开销限制，
    # 想再往上抬需要改 HTTP/1.1 + 正确 Content-Length。
    report["notes"] = {
        "protocol": "HTTP/1.0（无 keep-alive，每请求新建连接）",
        "implication": "吞吐受连接建立开销限制；改 HTTP/1.1 是一个明确的后续优化项",
    }

    print("\n".join(out))
    print("-" * 68)
    if failures:
        for f in failures:
            print(f"[FAIL] {f}", file=sys.stderr)
        print(f"\n[FAIL] 负载测试未通过（{len(failures)} 项）", file=sys.stderr)
    else:
        print("[OK] 负载测试通过：零错误、无丢数据、限流精确、指标自洽")

    rp = os.path.join(_HERE, "load_report.json")
    try:
        with open(rp, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        print(f"[load] 报告已写入：{rp}")
    except Exception as e:  # noqa: BLE001
        print(f"[warn] 报告写入失败：{e}", file=sys.stderr)

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())

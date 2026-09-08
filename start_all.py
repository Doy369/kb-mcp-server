#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
一键同时开启「前端 + 后端」：
  - 前端/REST 后端：app.py   -> http://localhost:8000 (自动开浏览器)
  - MCP 工具后端：  python -m kb_mcp_server (FastMCP, stdio 传输, 常驻后台)

用法：
  python start_all.py            # 启动前后端
  python start_all.py --stop     # 停止本脚本拉起的所有进程
  KB_WEB_PORT=9000 python start_all.py   # 换 Web 端口

说明：
  - 子进程复用本解释器(sys.executable)，保证依赖环境一致。
  - 日志写到 logs/web.log / logs/mcp.log，便于排查(避免输出被吞)。
  - Web 端口被占用时 app.py 会自检退出并提示，脚本会捕获并报告。
"""
import os
import sys
import re
import time
import signal
import atexit
import subprocess
import argparse
import webbrowser

ROOT = os.path.dirname(os.path.abspath(__file__))
WEB_PORT = int(os.getenv("PORT") or os.getenv("KB_WEB_PORT") or "8000")
LOGS_DIR = os.path.join(ROOT, "logs")
os.makedirs(LOGS_DIR, exist_ok=True)

# Windows 下让子进程不弹黑框；其它平台忽略
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

WEB_ARGS = [sys.executable, "app.py"]
MCP_ARGS = [sys.executable, "-m", "kb_mcp_server"]

# 记录本脚本拉起的子进程
_PROCS = []


def _log_path(name):
    return os.path.join(LOGS_DIR, name)


def _is_port_listening(port, host="127.0.0.1"):
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        return s.connect_ex((host, port)) == 0
    finally:
        s.close()


def _start_web():
    """启动前端 + REST 后端 (app.py)。"""
    web_log = open(_log_path("web.log"), "w", encoding="utf-8", buffering=1)
    p = subprocess.Popen(
        WEB_ARGS,
        cwd=ROOT,
        stdout=web_log,
        stderr=subprocess.STDOUT,
        creationflags=CREATE_NO_WINDOW,
    )
    _PROCS.append(("web", p))
    return p


def _start_mcp():
    """启动 MCP 工具后端 (stdio 常驻)。"""
    mcp_log = open(_log_path("mcp.log"), "w", encoding="utf-8", buffering=1)
    p = subprocess.Popen(
        MCP_ARGS,
        cwd=ROOT,
        stdout=mcp_log,
        stderr=subprocess.STDOUT,
        creationflags=CREATE_NO_WINDOW,
    )
    _PROCS.append(("mcp", p))
    return p


def _wait_web(timeout=15):
    for _ in range(timeout):
        if _is_port_listening(WEB_PORT):
            return True
        time.sleep(1)
    return False


def start():
    print(f"[启动] 准备同时开启前后端 (Web 端口={WEB_PORT}) ...")
    if _is_port_listening(WEB_PORT):
        print(f"[冲突] 端口 {WEB_PORT} 已被占用（多半是上次启动的旧实例），自动停止旧实例后重启...")
        stop()
        time.sleep(1)
    p_mcp = _start_mcp()
    print(f"[后端] MCP 工具服务已拉起 (pid={p_mcp.pid}, 日志={_log_path('mcp.log')})")

    p_web = _start_web()
    print(f"[前端] Web 控制台已拉起 (pid={p_web.pid}, 日志={_log_path('web.log')})")

    # 等 Web 起来
    print(f"[等待] 探测 http://localhost:{WEB_PORT} ...", end="", flush=True)
    if _wait_web(15):
        print(" OK")
        url = f"http://localhost:{WEB_PORT}/"
        try:
            webbrowser.open(url)
        except Exception:
            pass
        print(f"[完成] 前后端已开启 → {url}")
        print(f"[提示] 按 Ctrl+C 停止全部进程。")
    else:
        print(" 超时")
        # 检查 app.py 是否因端口冲突退出
        if p_web.poll() is not None:
            code = p_web.returncode
            print(f"[错误] app.py 已退出 (code={code})，请查看日志: {_log_path('web.log')}")
        else:
            print(f"[错误] Web 未在 15s 内就绪，请查看日志: {_log_path('web.log')}")

    _supervise()


def _supervise():
    """主进程常驻，等待 Ctrl+C；任一子进程异常退出则提示。"""
    try:
        while True:
            time.sleep(1)
            for tag, p in list(_PROCS):
                if p.poll() is not None:
                    print(f"[{tag}] 进程已退出 (code={p.returncode})")
    except KeyboardInterrupt:
        print("\n[停止] 收到 Ctrl+C，正在关闭前后端 ...")
        _stop_procs()


def _stop_procs():
    for tag, p in list(_PROCS):
        try:
            if p.poll() is None:
                p.terminate()
                p.wait(timeout=5)
        except Exception:
            try:
                p.kill()
            except Exception:
                pass
    print("[停止] 已完成。")


def stop():
    """停止本脚本拉起、或命令行中匹配的 app.py / kb_mcp_server 进程。"""
    print("[停止] 查找并终止 app.py / kb_mcp_server 进程 ...")
    killed = []
    # A) 精确杀掉占用 Web 端口的进程（避免误杀其它 python）
    try:
        net = subprocess.run(["netstat", "-ano"], capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=15).stdout or ""
        for ln in net.splitlines():
            if f":{WEB_PORT} " in ln and "LISTENING" in ln:
                m = re.search(r"(\d+)\s*$", ln.strip())
                if m:
                    pid = m.group(1)
                    try:
                        subprocess.run(["taskkill", "/F", "/PID", pid], capture_output=True, timeout=10)
                        killed.append(pid)
                    except Exception:
                        pass
    except Exception as e:
        print(f"[警告] netstat 查端口失败: {e}")
    # B) 再杀命令行含 app.py / kb_mcp_server 的 python 进程（含 MCP 后端）
    try:
        ps = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
             "Select-Object ProcessId,CommandLine | "
             "ForEach-Object { $_.ProcessId.ToString() + '||' + ($_.CommandLine -replace '\"','') }"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=25,
        ).stdout or ""
        for line in ps.splitlines():
            if "app.py" in line or "kb_mcp_server" in line:
                pid = line.split("||", 1)[0].strip()
                if pid.isdigit() and pid not in killed:
                    try:
                        subprocess.run(["taskkill", "/F", "/PID", pid], capture_output=True, timeout=10)
                        killed.append(pid)
                    except Exception:
                        pass
    except Exception as e:
        print(f"[警告] PowerShell 枚举失败 ({e})，回退 tasklist ...")
        try:
            tl = subprocess.run(["tasklist", "/FI", "IMAGENAME eq python.exe", "/FO", "CSV"],
                                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15).stdout or ""
            # tasklist CSV 不带命令行，无法精确匹配，仅记录
        except Exception as e2:
            print(f"[错误] 无法枚举进程: {e2}")
    if killed:
        print(f"[停止] 已终止进程: {', '.join(killed)}")
    else:
        print("[停止] 未发现匹配的 app.py / kb_mcp_server 进程。")


def main():
    parser = argparse.ArgumentParser(description="一键开启知识库前后端 (Web + MCP)")
    parser.add_argument("--stop", action="store_true", help="停止已拉起的进程")
    args = parser.parse_args()
    atexit.register(_stop_procs)
    if args.stop:
        stop()
    else:
        start()


if __name__ == "__main__":
    main()

"""桌面客户端入口：用原生窗口承载知识库控制台(复用 app.py 的本地 HTTP 服务)。

与"网站版"的区别：不再打开系统浏览器，而是用 pywebview 创建一个原生桌面窗口
(无地址栏/无标签页)，窗口里渲染的就是原来的 HTML/JS 控制台。后端仍是本地
ThreadingHTTPServer，所有接口、配置向导、检索可视化都不变。

打包：PyInstaller --onefile --noconsole --name kb-mcp-client，
      通过 --add-data 把 static/、kb_store.json、runtime_config.json 一并带进去。
"""

import ctypes
import os
import sys
import shutil
import threading
import socket
import webbrowser

import webview

from kb_mcp_server.config import get_settings, load_runtime_config, DATA_DIR
import app as appmod  # 复用 HTTP 服务 Handler 与静态目录解析逻辑


def _seed_defaults():
    """首次运行(冻结模式)：把打包内的默认数据复制到用户可写的数据目录。

    这样 6 篇示例文档与调参(runtime_config.json)会在 %APPDATA%/kb-mcp-server 落盘，
    用户改了配置、加了自己的知识库后，重启不丢。
    """
    if getattr(sys, "frozen", False) and DATA_DIR:
        for _f in ("kb_store.json", "runtime_config.json"):
            _src = os.path.join(sys._MEIPASS, _f)
            _dst = os.path.join(DATA_DIR, _f)
            if os.path.exists(_src) and not os.path.exists(_dst):
                try:
                    shutil.copyfile(_src, _dst)
                except Exception:
                    pass


def _free_port(start):
    """从 start 起找一个未被占用的本地端口，避免和已运行的实例冲突。"""
    p = start
    for _ in range(100):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind(("127.0.0.1", p))
            s.close()
            return p
        except OSError:
            p += 1
    return start  # 兜底：极端情况原样返回


def main():
    # 1) 首跑播种默认数据
    _seed_defaults()

    # 2) 加载配置(环境变量 + 运行时配置)
    get_settings()
    load_runtime_config()

    # 3) 在后台线程起本地 HTTP 服务(窗口加载的就是它)
    port = _free_port(int(os.getenv("KB_WEB_PORT", "8000")))
    server = appmod.ThreadingHTTPServer(("127.0.0.1", port), appmod.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    # 4) 打开原生窗口，承载控制台
    # 关键：在创建 WebView2 窗口前声明 DPI 感知。
    # 否则在高 DPI / 缩放屏上，WebView2 控件会被压缩成一条窄缝，
    # 只显示每行最右边的几个字、左侧整片白屏(就是你刚遇到的现象)。
    if sys.platform == "win32":
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)  # Per-Monitor Aware V2
        except Exception:
            try:
                ctypes.windll.user32.SetProcessDPIAware()  # 兜底
            except Exception:
                pass
    url = f"http://127.0.0.1:{port}/"
    webview.create_window(
        "企业客服知识库控制台",
        url,
        width=1280,
        height=820,
        min_size=(960, 640),
    )
    try:
        webview.start()  # 阻塞，直到窗口关闭
    except Exception:
        # 兜底：若目标机缺少 WebView2 运行库导致窗口建不起来，退回浏览器打开，
        # 避免"双击无任何反应"。
        webbrowser.open(url)

    # 5) 窗口关闭后停服(守护线程会随进程退出，这里显式收尾)
    try:
        server.shutdown()
    except Exception:
        pass


if __name__ == "__main__":
    main()

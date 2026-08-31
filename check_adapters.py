"""P4 外部 API 自检脚本。

填好 .env 里的 KB_ORDER_API_URL / KB_INVENTORY_API_URL（并设 KB_API_MOCK=0）后，
运行 `python check_adapters.py` 即可探测真实 endpoint，确认字段映射是否解析正确。

用法：
  python check_adapters.py
可选覆盖测试用主键：
  KB_ORDER_TEST_ID=SO123 KB_INVENTORY_TEST_SKU=SKU9 python check_adapters.py
"""
from kb_mcp_server.adapters import self_check


def main() -> None:
    report = self_check()
    print("=" * 60)
    print("P4 外部 API 适配器自检")
    print("=" * 60)
    print(f"全局 KB_API_MOCK = {report['mock']}  "
          f"（1=强制模拟；0 且配了 URL 才走真实 HTTP）\n")
    for a in report["adapters"]:
        print(f"[{a['adapter']}] 模式={a['mode']}")
        if a["mode"] == "mock":
            print(f"    -> 模拟模式：{a.get('note','')}")
            continue
        if a.get("ok"):
            incomplete = a.get("incomplete")
            print(f"    -> 真实调用 OK：{a['url']}")
            print(f"    解析字段：{a['parsed']}")
            print(f"    不完整(字段全空)? {'是 ⚠ 检查字段路径' if incomplete else '否'}")
        else:
            print(f"    -> 真实调用失败 ❌：{a['error']}")
            print(f"    请求 URL：{a['url']}")
        print()


if __name__ == "__main__":
    main()

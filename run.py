"""WIFI 直连 · 文件快传 —— 启动入口。

用法：
    python run.py                # 正常启动（窗口 + 服务）
    python run.py --headless     # 只起服务，不开窗口
    python run.py --port 9000    # 指定端口
    python run.py --ip 192.168.1.5
"""
from __future__ import annotations

import argparse
import os
import secrets
import socket
import sys
import tempfile
import threading
import time
import urllib.request

from wifilink import config, device, netutil
from wifilink import discovery as discovery_mod
from wifilink.server import create_app
from wifilink.store import store

LOG_PATH = os.path.join(tempfile.gettempdir(), "wifilink.log")


def setup_logging() -> None:
    """打包成 --windowed 后没有真正的控制台，输出统一落盘，避免编码/句柄报错。"""
    if getattr(sys, "frozen", False):
        handle = open(LOG_PATH, "a", encoding="utf-8", buffering=1)
        sys.stdout = handle
        sys.stderr = handle
        sys.__stdout__ = handle
        sys.__stderr__ = handle
        return
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001
            pass


def fatal(msg: str) -> None:
    print(msg, file=sys.stderr)
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(  # type: ignore[attr-defined]
            None, f"{msg}\n\n日志：{LOG_PATH}", "WIFI 直连 · 启动失败", 0x10)
    except Exception:  # noqa: BLE001
        pass


def pick_free_port(start: int = 8720) -> int:
    for port in range(start, start + 60):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("0.0.0.0", port))
                return port
            except OSError:
                continue
    raise RuntimeError("找不到可用端口")


def wait_until_ready(port: int, timeout: float = 8.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/session",
                                        timeout=0.5) as resp:
                if resp.status == 200:
                    return True
        except Exception:  # noqa: BLE001
            time.sleep(0.12)
    return False


def main() -> int:
    setup_logging()
    parser = argparse.ArgumentParser(description="WIFI 直连文件快传")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--ip", default="")
    parser.add_argument("--headless", action="store_true", help="不开窗口")
    parser.add_argument("--token", default="", help="固定访问口令（默认随机）")
    parser.add_argument("--discovery-port", type=int, default=discovery_mod.DISCOVERY_PORT,
                        help="局域网发现用的 UDP 端口（默认 8729）")
    parser.add_argument("--no-discovery", action="store_true",
                        help="关闭局域网自动发现（只保留扫码 / 手动连接）")
    parser.add_argument("--download-dir", default="",
                        help="接收目录；给了就写进配置并立即生效")
    args = parser.parse_args()

    # 命令行指定的目录优先：校验通过就落盘成配置，之后每次启动都读它
    if args.download_dir:
        ok, error, path = config.save_download_dir(args.download_dir)
        if ok:
            store.set_download_dir(path, "config")
        else:
            print(f"  [警告] --download-dir 不可用：{error}，继续使用 {store.download_dir}")

    port = args.port or pick_free_port()
    cards = netutil.nics()
    host_ip = args.ip or (cards[0].ip if cards else netutil.primary_ip())
    token = args.token or secrets.token_urlsafe(9)

    me = device.info()
    disc = None
    if not args.no_discovery:
        disc = discovery_mod.Discovery(port, me["id"], me["name"],
                                       udp_port=args.discovery_port)

    app = create_app(token, host_ip, port, discovery=disc)

    from waitress import serve
    server_thread = threading.Thread(
        target=lambda: serve(
            app,
            host="0.0.0.0",
            port=port,
            threads=12,
            channel_timeout=300,
            max_request_body_size=64 * 1024 ** 3,   # 允许超大文件
            clear_untrusted_proxy_headers=True,
        ),
        daemon=True,
        name="wifilink-http",
    )
    server_thread.start()

    if not wait_until_ready(port):
        fatal("服务启动失败：端口可能被占用，或被防火墙/安全软件拦截。")
        return 1

    if disc is not None:
        disc.start()

    url = f"http://{host_ip}:{port}/j/{token}"
    local_url = f"http://127.0.0.1:{port}/desktop"
    pick = next((c for c in cards if c.ip == host_ip), None)
    skipped = [f"{c.ip}（{c.name}）" for c in cards if c.virtual]

    bar = "─" * 58
    print(f"\n{bar}")
    print("  WIFI 直连 · 文件快传")
    print(f"{bar}")
    print(f"  手机扫码后访问：{url}")
    print(f"  本机控制台：    {local_url}")
    print(f"  本机名称：      {me['name']}（{me['id'][:8]}…）")
    print(f"  接收目录：      {store.download_dir}")
    if disc is not None:
        state = "已开启" if not disc.error else f"不可用（{disc.error}）"
        print(f"  局域网发现：    {state}，UDP {args.discovery_port}")
    else:
        print("  局域网发现：    已关闭（--no-discovery）")
    if pick:
        print(f"  所用网卡：      {pick.name}（{pick.ip}，网关 {pick.gateway or '无'}）")
    print(f"  本机可用地址：  {', '.join(c.ip for c in cards) or '(' + host_ip + ')'}")
    print(f"{bar}")
    if skipped:
        print(f"  已跳过虚拟网卡：{', '.join(skipped)}")
        print("  （VPN / 虚拟机 / WSL 的地址手机连不上，所以不作为默认值）")
    if pick and pick.virtual:
        print("  [注意] 当前唯一的地址来自虚拟网卡，手机可能连不上：")
        print("         请先连上和手机同一个 Wi-Fi，再重启本程序。")
    if store.dir_error:
        print(f"  [警告] {store.dir_error}")
    print(f"  配置文件：      {config.config_path()}")
    print("  提示：首次启动若弹出 Windows 防火墙，请允许「专用网络」访问。")
    print(f"{bar}\n")

    if args.headless:
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            return 0

    from wifilink.desktop import open_window
    open_window(local_url)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        fatal(f"程序异常退出：{exc}")

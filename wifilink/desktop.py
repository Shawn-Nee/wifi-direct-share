"""桌面窗口。

Windows 上用 Edge 的应用窗口模式（--app）承载界面：无地址栏、无标签页，
独立进程、关闭即退出。之所以不走 pywebview：它的 .NET WebView2 绑定
与较新的 WebView2 Runtime 存在 COM 接口不兼容（E_NOINTERFACE），
在部分机器上直接创建不出窗口。
"""
from __future__ import annotations

import ctypes
import os
import shutil
import subprocess
import sys
import tempfile
import webbrowser
from ctypes import wintypes

WINDOW_SIZE = (1100, 780)


def _dip_scale() -> float:
    """系统 DPI 缩放（125% → 1.25）。

    Edge 的 --window-size / --window-position 用的是 DIP，不是物理像素，
    所以必须先拿到缩放比，否则 200% 缩放下算出来的位置会偏出一倍。
    """
    user32 = ctypes.windll.user32
    dpi = 96
    try:
        dpi = user32.GetDpiForSystem() or 96
    except AttributeError:                      # Win10 1607 之前没有这个函数
        try:
            hdc = user32.GetDC(0)
            dpi = ctypes.windll.gdi32.GetDeviceCaps(hdc, 88) or 96  # LOGPIXELSX
            user32.ReleaseDC(0, hdc)
        except Exception:  # noqa: BLE001
            dpi = 96
    return dpi / 96.0


def _work_area() -> tuple[int, int, int, int]:
    """主显示器工作区（已扣掉任务栏），物理像素。"""
    rect = wintypes.RECT()
    # SPI_GETWORKAREA = 0x0030
    if ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0):
        return (rect.left, rect.top,
                max(0, rect.right - rect.left), max(0, rect.bottom - rect.top))
    get = ctypes.windll.user32.GetSystemMetrics
    return 0, 0, get(0), get(1)                 # SM_CXSCREEN / SM_CYSCREEN


def _window_args(size: tuple[int, int] = WINDOW_SIZE) -> list[str]:
    """算出居中于当前屏幕的工作区尺寸与位置（DIP）。"""
    from .screen import _ensure_dpi_aware

    _ensure_dpi_aware()                          # 先声明 DPI 感知，量出来才是物理像素
    left, top, area_w, area_h = _work_area()
    scale = _dip_scale() or 1.0
    area_w /= scale
    area_h /= scale

    w, h = size
    if w > area_w - 20 or h > area_h - 20:       # 小屏笔记本上别顶天立地
        w = int(min(w, max(640, area_w - 40)))
        h = int(min(h, max(480, area_h - 40)))

    x = left / scale + max(0.0, (area_w - w) / 2)
    y = top / scale + max(0.0, (area_h - h) / 2)
    return [f"--window-size={int(w)},{int(h)}",
            f"--window-position={int(round(x))},{int(round(y))}"]


def _find_edge() -> str | None:
    local = os.environ.get("LOCALAPPDATA", "")
    candidates = [
        os.path.join(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"),
                     r"Microsoft\Edge\Application\msedge.exe"),
        os.path.join(os.environ.get("PROGRAMFILES", r"C:\Program Files"),
                     r"Microsoft\Edge\Application\msedge.exe"),
        os.path.join(local, r"Microsoft\Edge\Application\msedge.exe"),
    ]
    for path in candidates:
        if path and os.path.isfile(path):
            return path
    return shutil.which("msedge")


def _profile_dir() -> str:
    base = os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
    path = os.path.join(base, "WIFILink", "edge-profile")
    os.makedirs(path, exist_ok=True)
    return path


def open_window(url: str) -> str:
    """以应用窗口打开界面，返回使用的模式。阻塞直到窗口关闭。"""
    edge = _find_edge()
    if edge:
        args = [
            edge,
            f"--app={url}",
            f"--user-data-dir={_profile_dir()}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-features=msEdgeSidebarV2,msEdgeTabPreloading",
            *_window_args(),
        ]
        try:
            proc = subprocess.Popen(args)
        except OSError as exc:
            print(f"[warn] 启动 Edge 失败：{exc}", file=sys.stderr)
        else:
            try:
                # 若 3 秒内进程就退出，说明它把请求转交给了已在运行的实例
                proc.wait(timeout=3.0)
                print("[info] 界面已交给已有的 Edge 实例，服务继续运行。",
                      file=sys.stderr)
                _idle()
                return "edge-app"
            except subprocess.TimeoutExpired:
                proc.wait()   # 正常情况下窗口一关闭就返回
                return "edge-app"

    webbrowser.open(url)
    print("[info] 已用默认浏览器打开界面。", file=sys.stderr)
    _idle()
    return "browser"


def _idle() -> None:
    import time
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        return

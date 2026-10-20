"""屏幕截取。

设计取舍：
- 用 Pillow 的 ImageGrab（底层 BitBlt），不依赖任何 GUI 框架。
  托管版 Python 不带 tkinter，装 Qt 又会让 exe 膨胀一百多兆，不值得。
- 抓屏前把自己那个 Edge 应用窗口最小化、抓完立刻恢复，
  否则每张截图都会盖住自己，用户还得手动裁掉。
- 选区交互放在网页里做（拖拽 → 传坐标 → 服务端裁切），
  这样不用自己实现一个置顶的透明覆盖窗，跨 DPI / 多屏也更稳。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import os
import secrets
import tempfile
import threading
import time

from PIL import Image, ImageGrab

SHOT_DIR = os.path.join(tempfile.gettempdir(), "wifilink_shots")
WINDOW_TITLE = "WIFI 直连"

# 待发送的截图：sid -> 文件路径。抓完还没裁切的图放这里，发送或取消后即删。
_shots: dict[str, dict] = {}
_lock = threading.Lock()

_dpi_ready = False


def _ensure_dpi_aware() -> None:
    """声明进程 DPI 感知，否则多屏 / 高缩放下抓到的是一块被系统缩放过的糊图。"""
    global _dpi_ready
    if _dpi_ready:
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PER_MONITOR_DPI_AWARE
    except Exception:  # noqa: BLE001
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:  # noqa: BLE001
            pass
    _dpi_ready = True


# ---------- 把自己那个窗口临时藏起来 ----------
def _find_own_window(hint: str | None) -> int:
    """按标题找自己的窗口。

    hint 是本实例的端口标记，形如 "(8720)"。必传且必须命中：
    标题里不带端口的窗口可能是另一个正在运行的实例，绝不能最小化它。
    """
    if not hint:
        return 0
    user32 = ctypes.windll.user32
    hits: list[int] = []

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def _cb(hwnd, _lparam):
        if user32.IsWindowVisible(hwnd):
            n = user32.GetWindowTextLengthW(hwnd)
            if n:
                buf = ctypes.create_unicode_buffer(n + 1)
                user32.GetWindowTextW(hwnd, buf, n + 1)
                if buf.value.startswith(WINDOW_TITLE) and hint in buf.value:
                    hits.append(int(hwnd))
        return True

    try:
        user32.EnumWindows(_cb, 0)
    except Exception:  # noqa: BLE001
        return 0
    return hits[0] if hits else 0


def grab(hide_hint: str | None = None) -> Image.Image:
    """抓整个虚拟桌面（含所有显示器）。"""
    _ensure_dpi_aware()
    hwnd = _find_own_window(hide_hint)
    user32 = ctypes.windll.user32 if hwnd else None
    try:
        if hwnd:
            user32.ShowWindow(hwnd, 6)          # SW_MINIMIZE
            time.sleep(0.28)                     # 等最小化动画放完，否则会截到半透明残影
        img = ImageGrab.grab(all_screens=True)
    finally:
        if hwnd:
            user32.ShowWindow(hwnd, 9)          # SW_RESTORE
            user32.SetForegroundWindow(hwnd)
    return img.convert("RGB")


# ---------- 会话：抓完先存着，等前端给裁切坐标 ----------
def _purge_stale(max_age: float = 900.0) -> None:
    cutoff = time.time() - max_age
    for sid, meta in list(_shots.items()):
        if meta["at"] < cutoff:
            _drop_file(meta["path"])
            _shots.pop(sid, None)


def _drop_file(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def capture(hide_hint: str | None = None) -> dict:
    """抓一张全屏暂存，返回 {id, w, h}。"""
    os.makedirs(SHOT_DIR, exist_ok=True)
    img = grab(hide_hint)
    sid = secrets.token_hex(6)
    path = os.path.join(SHOT_DIR, f"{sid}.png")
    img.save(path, "PNG", optimize=True)
    with _lock:
        _purge_stale()
        _shots[sid] = {"path": path, "w": img.width, "h": img.height,
                       "at": time.time()}
    return {"id": sid, "w": img.width, "h": img.height,
            "size": os.path.getsize(path)}


def temp_path(sid: str) -> str | None:
    with _lock:
        meta = _shots.get(sid)
        return meta["path"] if meta else None


def commit(sid: str, box: tuple[int, int, int, int] | None, out_dir: str,
           stem: str) -> tuple[str, str] | None:
    """按选区裁切并落盘到 out_dir，返回 (最终文件名, 路径)。box 为 None 表示整屏。"""
    with _lock:
        meta = _shots.pop(sid, None)
    if meta is None:
        return None
    src = meta["path"]
    try:
        with Image.open(src) as im:
            im = im.convert("RGB")
            if box is None:
                cropped = im
            else:
                x, y, w, h = box
                x = max(0, min(x, im.width - 1))
                y = max(0, min(y, im.height - 1))
                w = max(1, min(w, im.width - x))
                h = max(1, min(h, im.height - y))
                cropped = im.crop((x, y, x + w, y + h))
            os.makedirs(out_dir, exist_ok=True)
            name = _unique(out_dir, f"{stem}.png")
            dest = os.path.join(out_dir, name)
            cropped.save(dest, "PNG", optimize=True)
    except Exception:  # noqa: BLE001
        _drop_file(src)
        raise
    _drop_file(src)
    return name, dest


def discard(sid: str) -> bool:
    with _lock:
        meta = _shots.pop(sid, None)
    if meta is None:
        return False
    _drop_file(meta["path"])
    return True


def _unique(directory: str, name: str) -> str:
    base, ext = os.path.splitext(name)
    candidate = name
    n = 1
    while os.path.exists(os.path.join(directory, candidate)):
        candidate = f"{base} ({n}){ext}"
        n += 1
    return candidate

"""屏幕截取。

设计取舍：
- 用 Pillow 的 ImageGrab（底层 BitBlt），不依赖任何 GUI 框架。
  托管版 Python 不带 tkinter，装 Qt 又会让 exe 膨胀一百多兆，不值得。
- 抓屏前把自己那个 Edge 应用窗口最小化，并且**抓完不立刻恢复**：
  接着要开一个整屏的框选覆盖窗（见 desktop.open_shot_window），
  主窗口要是一直立在那儿，覆盖层背后就会透出自己，等于没截干净。
  所以恢复拆成独立的 restore_main()，由发送 / 取消 / 关窗时调用。
- 框选交互放在覆盖层页面里做（全屏显示原图 → 传原图像素坐标 → 服务端裁切），
  不自己实现置顶透明窗，跨 DPI / 多屏更稳。
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
SHOT_WINDOW_TITLE = "框选截图"

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
def _find_window(prefix: str, hint: str | None) -> int:
    """按标题前缀 + 端口标记找窗口。

    hint 是本实例的端口标记，形如 "(8720)"。必传且必须命中：
    标题里不带端口的窗口可能是另一个正在运行的实例，绝不能动它。
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
                if buf.value.startswith(prefix) and hint in buf.value:
                    hits.append(int(hwnd))
        return True

    try:
        user32.EnumWindows(_cb, 0)
    except Exception:  # noqa: BLE001
        return 0
    return hits[0] if hits else 0


def _find_own_window(hint: str | None) -> int:
    return _find_window(WINDOW_TITLE, hint)


def _restore_window(hwnd: int) -> None:
    user32 = ctypes.windll.user32
    user32.ShowWindow(hwnd, 9)              # SW_RESTORE
    try:
        user32.SetForegroundWindow(hwnd)
    except Exception:  # noqa: BLE001
        pass


def restore_main(hint: str | None) -> bool:
    """把主窗口从最小化里放回来。截图结束（发送 / 取消 / 关窗）都要调一次。"""
    hwnd = _find_own_window(hint)
    if not hwnd:
        return False
    _restore_window(hwnd)
    return True


def raise_window(hwnd: int) -> bool:
    """把窗口置顶并抢到前台。

    由本进程 Popen 出来的 Edge 窗口默认拿不到前台权限（Windows 的前台锁定），
    实测它会排在已经激活的窗口后面 —— 用户看到的还是原来的桌面，白截一张。
    所以先 HWND_TOPMOST 压住所有普通窗口，再靠 AttachThreadInput
    把自己的输入队列挂到当前前台线程上，才抢得动焦点。
    """
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    user32.SetWindowPos.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                    ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                    ctypes.c_int, wt.UINT]
    user32.SetWindowPos.restype = wt.BOOL
    user32.GetWindowThreadProcessId.restype = wt.DWORD
    user32.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p,
                                                ctypes.POINTER(wt.DWORD)]

    HWND_TOPMOST = ctypes.c_void_p(-1)
    flags = 0x0001 | 0x0002 | 0x0040            # NOSIZE | NOMOVE | SHOWWINDOW
    user32.SetWindowPos(ctypes.c_void_p(hwnd), HWND_TOPMOST, 0, 0, 0, 0, flags)

    tid_me = kernel32.GetCurrentThreadId()
    fg = user32.GetForegroundWindow()
    tid_fg = 0
    if fg:
        pid = wt.DWORD()
        tid_fg = user32.GetWindowThreadProcessId(ctypes.c_void_p(fg),
                                                 ctypes.byref(pid))
    attached = False
    if tid_fg and tid_fg != tid_me:
        attached = bool(user32.AttachThreadInput(wt.DWORD(tid_me),
                                                 wt.DWORD(tid_fg), True))
    try:
        user32.SetForegroundWindow(ctypes.c_void_p(hwnd))
        user32.BringWindowToTop(ctypes.c_void_p(hwnd))
    finally:
        if attached:
            user32.AttachThreadInput(wt.DWORD(tid_me), wt.DWORD(tid_fg), False)
    return True


def close_shot_window(hint: str | None) -> bool:
    """关掉框选覆盖层。页面自己 window.close() 不一定被允许，这里兜一手。"""
    hwnd = _find_window(SHOT_WINDOW_TITLE, hint)
    if not hwnd:
        return False
    user32 = ctypes.windll.user32
    user32.PostMessageW.argtypes = [ctypes.c_void_p, wt.UINT,
                                    ctypes.c_void_p, ctypes.c_void_p]
    user32.PostMessageW.restype = wt.BOOL
    user32.PostMessageW(ctypes.c_void_p(hwnd), 0x0010, 0, 0)   # WM_CLOSE
    return True


def grab(hide_hint: str | None = None, restore: bool = True) -> Image.Image:
    """抓整个虚拟桌面（含所有显示器）。

    restore=False 时主窗口保持最小化，交给调用方在框选结束后 restore_main()。
    """
    _ensure_dpi_aware()
    hwnd = _find_own_window(hide_hint)
    user32 = ctypes.windll.user32 if hwnd else None
    try:
        if hwnd:
            user32.ShowWindow(hwnd, 6)          # SW_MINIMIZE
            time.sleep(0.28)                     # 等最小化动画放完，否则会截到半透明残影
        img = ImageGrab.grab(all_screens=True)
    finally:
        if hwnd and restore:
            _restore_window(hwnd)
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
    """抓一张全屏暂存，返回 {id, w, h}。

    抓完主窗口**保持最小化**：紧跟着要开整屏框选覆盖层，
    主窗口要是弹回来就会盖在桌面内容上。恢复由框选结束时的 restore_main() 负责。
    """
    os.makedirs(SHOT_DIR, exist_ok=True)
    try:
        img = grab(hide_hint, restore=False)
    except Exception:  # noqa: BLE001
        restore_main(hide_hint)              # 抓屏失败别把主窗口丢在最小化里
        raise
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

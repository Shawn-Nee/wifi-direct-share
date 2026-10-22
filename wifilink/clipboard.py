"""往 Windows 剪贴板里放东西：文件（CF_HDROP）和图片（CF_DIB）。

为什么不用 `clip.exe` 或 PowerShell 的 Set-Clipboard：
- `clip` 只吃文本，塞不了文件、塞不了图片；
- `Set-Clipboard -Path` 要 Win10 1809 起才有，且每次拉起一个进程，
  慢、带一闪而过的控制台窗口，还得处理同步时机。
直接写格式就是各程序自己的格式：

- CF_HDROP = 资源管理器 Ctrl+C 的那份数据，目标程序（微信、钉钉、Word、
  资源管理器、邮件客户端）取到的就是「一个文件」，粘贴行为与系统完全一致。
- CF_DIB = 剪贴板里的位图，粘贴到聊天窗口 / 文档 / 画图里就是一张图本身。
  不用 CF_HDROP 代替：那样贴进微信是个「待发送的文件」，不是可以直接发出去的图。
"""
from __future__ import annotations

import ctypes
import io
import os
import time
from ctypes import wintypes

from PIL import Image

CF_HDROP = 15
CF_DIB = 8
GMEM_MOVEABLE = 0x0002
BI_RGB = 0

# 96 dpi 换算成「像素/米」，只是给 DIB 头填个像样的值，
# 有些老程序会拿它算「打印出来多大」。
PELS_PER_METER = 3780

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_shell32 = ctypes.WinDLL("shell32", use_last_error=True)


class ClipboardError(RuntimeError):
    """文案直接面向用户，调用方原样展示即可。"""


class _DROPFILES(ctypes.Structure):
    """DROPFILES 结构体后面紧跟一串以 \\0 分隔、双 \\0 结尾的文件名。"""

    _fields_ = [
        ("pFiles", wintypes.DWORD),     # 文件名区相对本结构体开头的偏移
        ("pt", wintypes.POINT),         # 拖放时的鼠标位置，粘贴场景用不上
        ("fNC", wintypes.BOOL),
        ("fWide", wintypes.BOOL),       # TRUE = 宽字符（UTF-16LE）
    ]


class _BITMAPINFOHEADER(ctypes.Structure):
    """CF_DIB 的开头就是它，后面紧跟像素数据（BITMAPINFOHEADER，40 字节）。"""

    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", ctypes.c_long),
        ("biHeight", ctypes.c_long),        # 正数表示自下而上存行
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),  # BI_RGB = 0，即不压缩
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", ctypes.c_long),
        ("biYPelsPerMeter", ctypes.c_long),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


def _open(retries: int = 12, delay: float = 0.035) -> bool:
    """抢剪贴板：别的程序（输入法、剪贴板管理器）可能正占着，短暂重试。"""
    for _ in range(retries):
        if _user32.OpenClipboard(None):
            return True
        time.sleep(delay)
    return False


def _clip_write(fmt: int, payload: bytes, what: str) -> None:
    """一个事务里把 payload 以 fmt 格式放进剪贴板：开 → 清空 → 写入 → 关。

    失败一律抛 ClipboardError，文案直接面向用户。
    """
    _kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
    _kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    _kernel32.GlobalLock.restype = ctypes.c_void_p
    _kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    _kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    _kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
    _user32.SetClipboardData.restype = wintypes.HANDLE
    _user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    _user32.EmptyClipboard.restype = wintypes.BOOL
    _user32.OpenClipboard.argtypes = [wintypes.HWND]

    if not _open():
        err = ctypes.get_last_error()
        if err == 5:  # ERROR_ACCESS_DENIED：被别的进程占着
            raise ClipboardError("剪贴板正被其他程序占用，稍等一秒再试")
        raise ClipboardError(f"打不开剪贴板（错误码 {err}）")

    hmem = None
    try:
        if not _user32.EmptyClipboard():
            raise ClipboardError(f"清空剪贴板失败（错误码 {ctypes.get_last_error()}）")

        hmem = _kernel32.GlobalAlloc(GMEM_MOVEABLE, len(payload))
        if not hmem:
            raise ClipboardError(f"申请内存失败，{what}太大或内存不足")

        ptr = _kernel32.GlobalLock(hmem)
        if not ptr:
            raise ClipboardError("锁定内存失败")
        ctypes.memmove(ptr, payload, len(payload))
        _kernel32.GlobalUnlock(hmem)

        if not _user32.SetClipboardData(fmt, hmem):
            raise ClipboardError(f"写入剪贴板失败（错误码 {ctypes.get_last_error()}）")
        hmem = None      # 所有权已交给系统，自己绝不能再 GlobalFree
    finally:
        _user32.CloseClipboard()
        if hmem:
            _kernel32.GlobalFree(hmem)


def copy_files(paths: list[str]) -> None:
    """把一串文件放到剪贴板上，失败抛 ClipboardError（带中文原因）。"""
    files = [os.path.abspath(p) for p in paths if p]
    if not files:
        raise ClipboardError("没有可复制的文件")

    for path in files:
        if not os.path.exists(path):
            raise ClipboardError(f"文件已被移动或删除：{os.path.basename(path)}")
        if not os.path.isfile(path):
            raise ClipboardError(f"不是文件：{os.path.basename(path)}")
        if not os.access(path, os.R_OK):
            raise ClipboardError(f"没有读取权限：{os.path.basename(path)}")

    head = _DROPFILES()
    head.pFiles = ctypes.sizeof(_DROPFILES)
    head.fWide = True
    # 每个路径以 \0 结尾，整串再补一个 \0 结束
    names = ("\0".join(files) + "\0\0").encode("utf-16-le")
    _clip_write(CF_HDROP, bytes(head) + names, "文件")


def copy_image(src: str | os.PathLike[str] | Image.Image) -> None:
    """把一张图放进剪贴板（CF_DIB），之后在别的软件里 Ctrl+V 就是这张图本身。

    src 传图片路径或已打开的 PIL.Image 都行。截屏流程里直接用刚裁好的文件，
    避免为了粘贴再走一遍编码。
    """
    if isinstance(src, Image.Image):
        payload = _dib_payload(src)
    else:
        path = os.fspath(src)
        if not os.path.isfile(path):
            raise ClipboardError("截图文件已被移动或删除")
        try:
            with Image.open(path) as im:
                payload = _dib_payload(im)
        except OSError as exc:
            raise ClipboardError(f"读不出截图内容：{exc}") from exc
    _clip_write(CF_DIB, payload, "图片")


def _dib_payload(image: Image.Image) -> bytes:
    """按 CF_DIB 的规矩拼字节：BITMAPINFOHEADER + 自下而上的 BGRX 行。"""
    im = image if image.mode == "RGBA" else image.convert("RGBA")
    w, h = im.size
    if not w or not h:
        raise ClipboardError("图片是空的，没有可复制的内容")

    head = _BITMAPINFOHEADER()
    head.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
    head.biWidth = w
    head.biHeight = h            # 正数：自下而上
    head.biPlanes = 1
    head.biBitCount = 32
    head.biCompression = BI_RGB  # 32 位 BI_RGB 按 BGRX 解释
    head.biSizeImage = w * h * 4
    head.biXPelsPerMeter = PELS_PER_METER
    head.biYPelsPerMeter = PELS_PER_METER

    # 一定要带 alpha=255：RGB 转 RGBA 会自动补 255，
    # 而 Pillow 的 BGRX 编码把 X 写成 0，某些程序会当成全透明，粘出来是一张空白。
    raw = memoryview(im.tobytes("raw", "BGRA"))
    stride = w * 4
    buf = io.BytesIO()
    buf.write(bytes(head))
    for y in range(h - 1, -1, -1):
        # 32 位每行天然 4 字节对齐，不用像 24 位那样补 padding
        buf.write(raw[y * stride:(y + 1) * stride])
    return buf.getvalue()


def read_files() -> list[str]:
    """读回剪贴板里的文件路径（自测与排障用，界面上用不到）。"""
    if not _open():
        return []
    try:
        _user32.GetClipboardData.restype = wintypes.HANDLE
        _user32.GetClipboardData.argtypes = [wintypes.UINT]
        handle = _user32.GetClipboardData(CF_HDROP)
        if not handle:
            return []
        _shell32.DragQueryFileW.restype = wintypes.UINT
        _shell32.DragQueryFileW.argtypes = [wintypes.HANDLE, wintypes.UINT,
                                            wintypes.LPWSTR, wintypes.UINT]
        count = _shell32.DragQueryFileW(handle, 0xFFFFFFFF, None, 0)
        out = []
        for i in range(count):
            need = _shell32.DragQueryFileW(handle, i, None, 0)
            buf = ctypes.create_unicode_buffer(need + 1)
            _shell32.DragQueryFileW(handle, i, buf, need + 1)
            out.append(buf.value)
        return out
    finally:
        _user32.CloseClipboard()

"""把文件放进 Windows 剪贴板（CF_HDROP），等价于在资源管理器里对文件按 Ctrl+C。

为什么不用 `clip.exe` 或 PowerShell 的 Set-Clipboard：
- `clip` 只吃文本，塞不了文件；
- `Set-Clipboard -Path` 要 Win10 1809 起才有，且每次拉起一个进程，
  慢、带一闪而过的控制台窗口，还得处理同步时机。
直接写 CF_HDROP 就是资源管理器自己的格式：目标程序（微信、钉钉、Word、
资源管理器、邮件客户端）取到的就是「一个文件」，粘贴行为与系统完全一致。
"""
from __future__ import annotations

import ctypes
import os
import time
from ctypes import wintypes

CF_HDROP = 15
GMEM_MOVEABLE = 0x0002

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


def _open(retries: int = 12, delay: float = 0.035) -> bool:
    """抢剪贴板：别的程序（输入法、剪贴板管理器）可能正占着，短暂重试。"""
    for _ in range(retries):
        if _user32.OpenClipboard(None):
            return True
        time.sleep(delay)
    return False


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
    payload = bytes(head) + names

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
            raise ClipboardError("申请内存失败，文件太大或内存不足")

        ptr = _kernel32.GlobalLock(hmem)
        if not ptr:
            raise ClipboardError("锁定内存失败")
        ctypes.memmove(ptr, payload, len(payload))
        _kernel32.GlobalUnlock(hmem)

        if not _user32.SetClipboardData(CF_HDROP, hmem):
            raise ClipboardError(f"写入剪贴板失败（错误码 {ctypes.get_last_error()}）")
        hmem = None      # 所有权已交给系统，自己绝不能再 GlobalFree
    finally:
        _user32.CloseClipboard()
        if hmem:
            _kernel32.GlobalFree(hmem)


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

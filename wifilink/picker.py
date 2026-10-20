"""调用 Windows 原生文件/文件夹选择框（comdlg32 / shell32）。

为什么不用 tkinter：tkinter 的 filedialog 底层也是这个 API，
但会额外拉起一套 Tcl/Tk 运行时，打包体积和线程问题都不划算。
"""
from __future__ import annotations

import ctypes
import os
from ctypes import wintypes

OFN_READONLY = 0x00000001
OFN_NOCHANGEDIR = 0x00000008
OFN_ALLOWMULTISELECT = 0x00000200
OFN_EXPLORER = 0x00080000
OFN_FILEMUSTEXIST = 0x00001000
OFN_PATHMUSTEXIST = 0x00000800
OFN_NODEREFERENCELINKS = 0x00100000

MAX_PATH_BUF = 65536

# 文件夹选择框（shell32!SHBrowseForFolder）
BIF_RETURNONLYFSDIRS = 0x0001
BIF_EDITBOX = 0x0010
BIF_NEWDIALOGSTYLE = 0x0040

# 回调消息
BFFM_INITIALIZED = 0x0001
BFFM_SETSELECTIONW = 0x0400 + 103   # WM_USER + 103


class BROWSEINFOW(ctypes.Structure):
    _fields_ = [
        ("hwndOwner", wintypes.HWND),
        ("pidlRoot", ctypes.c_void_p),
        ("pszDisplayName", wintypes.LPWSTR),
        ("lpszTitle", wintypes.LPCWSTR),
        ("ulFlags", wintypes.UINT),
        ("lpfn", ctypes.c_void_p),
        ("lParam", wintypes.LPARAM),
        ("iImage", ctypes.c_int),
    ]


class OPENFILENAMEW(ctypes.Structure):
    _fields_ = [
        ("lStructSize", wintypes.DWORD),
        ("hwndOwner", wintypes.HWND),
        ("hInstance", wintypes.HINSTANCE),
        ("lpstrFilter", wintypes.LPCWSTR),
        ("lpstrCustomFilter", wintypes.LPWSTR),
        ("nMaxCustFilter", wintypes.DWORD),
        ("nFilterIndex", wintypes.DWORD),
        ("lpstrFile", wintypes.LPWSTR),
        ("nMaxFile", wintypes.DWORD),
        ("lpstrFileTitle", wintypes.LPWSTR),
        ("nMaxFileTitle", wintypes.DWORD),
        ("lpstrInitialDir", wintypes.LPCWSTR),
        ("lpstrTitle", wintypes.LPCWSTR),
        ("Flags", wintypes.DWORD),
        ("nFileOffset", wintypes.WORD),
        ("nFileExtension", wintypes.WORD),
        ("lpstrDefExt", wintypes.LPCWSTR),
        ("lCustData", wintypes.LPARAM),
        ("lpfnHook", ctypes.c_void_p),
        ("lpTemplateName", wintypes.LPCWSTR),
        ("pvReserved", ctypes.c_void_p),
        ("dwReserved", wintypes.DWORD),
        ("FlagsEx", wintypes.DWORD),
    ]


def _owner_hwnd() -> int:
    try:
        return ctypes.windll.user32.GetForegroundWindow() or 0
    except Exception:  # noqa: BLE001
        return 0


def pick_files(title: str = "选择要发送的文件") -> list[str]:
    """弹出多选文件对话框，返回绝对路径列表；用户取消返回空列表。"""
    if os.name != "nt":
        return []

    try:
        ctypes.windll.ole32.CoInitialize(None)
    except Exception:  # noqa: BLE001
        pass

    buf = ctypes.create_unicode_buffer(MAX_PATH_BUF)
    ofn = OPENFILENAMEW()
    ofn.lStructSize = ctypes.sizeof(OPENFILENAMEW)
    ofn.hwndOwner = _owner_hwnd()
    # 过滤器：所有文件 + 常见类型，双 \0 结尾
    ofn.lpstrFilter = "所有文件\0*.*\0图片\0*.jpg;*.jpeg;*.png;*.gif;*.webp;*.heic;*.bmp\0视频\0*.mp4;*.mov;*.mkv;*.avi\0文档\0*.pdf;*.doc;*.docx;*.xls;*.xlsx;*.ppt;*.pptx;*.txt\0\0"
    ofn.lpstrFile = ctypes.cast(buf, wintypes.LPWSTR)
    ofn.nMaxFile = MAX_PATH_BUF
    ofn.lpstrTitle = title
    ofn.Flags = (OFN_EXPLORER | OFN_FILEMUSTEXIST | OFN_PATHMUSTEXIST
                 | OFN_ALLOWMULTISELECT | OFN_NOCHANGEDIR
                 | OFN_NODEREFERENCELINKS)
    ofn.nFilterIndex = 1

    ok = ctypes.windll.comdlg32.GetOpenFileNameW(ctypes.byref(ofn))
    if not ok:
        return []

    raw = buf[:]
    parts = [p for p in raw.split("\0") if p]
    if not parts:
        return []
    if len(parts) == 1:
        return [os.path.abspath(parts[0])]
    directory = parts[0]
    return [os.path.abspath(os.path.join(directory, p)) for p in parts[1:]]


def _nearest_dir(path: str) -> str:
    """取路径本身或最近一个存在的父目录。"""
    probe = os.path.abspath(path) if path else ""
    while probe and not os.path.isdir(probe):
        parent = os.path.dirname(probe.rstrip("\\/"))
        if parent == probe:
            return ""
        probe = parent
    return probe


def pick_folder(title: str = "选择接收文件夹", initial: str = "") -> str:
    """弹出原生文件夹选择框，返回绝对路径；用户取消返回空串。

    优先用 IFileOpenDialog（Win7 起资源管理器的选择框：面包屑、地址栏、
    新建文件夹齐全，任何盘任何层级都能点到）；起不来才退回 SHBrowseForFolder。
    """
    if os.name != "nt":
        return ""

    start = _nearest_dir(initial)
    try:
        got = _pick_folder_modern(title, start)
    except Exception:  # noqa: BLE001
        got = None                      # 现代接口出问题就退老框，不让用户卡住
    if got is not None:
        return got
    return _pick_folder_tree(title, start)


def _pick_folder_tree(title: str, start: str) -> str:
    """兜底：SHBrowseForFolder + BIF_NEWDIALOGSTYLE（老式树形框）。

    树的根必须是桌面（pidlRoot = NULL）。若把 pidlRoot 指向起始目录，
    整棵树就以那个目录为根，用户连它的上一级都点不到。
    """
    try:
        ctypes.windll.ole32.CoInitialize(None)
    except Exception:  # noqa: BLE001
        pass

    shell32 = ctypes.windll.shell32
    user32 = ctypes.windll.user32
    # 关键：pidl 是 64 位指针，不声明 restype 会被 ctypes 按 int 截断
    shell32.SHBrowseForFolderW.restype = ctypes.c_void_p
    shell32.SHBrowseForFolderW.argtypes = [ctypes.POINTER(BROWSEINFOW)]
    shell32.SHGetPathFromIDListW.restype = wintypes.BOOL
    shell32.SHGetPathFromIDListW.argtypes = [ctypes.c_void_p, wintypes.LPWSTR]
    user32.SendMessageW.restype = wintypes.LPARAM
    user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT,
                                    wintypes.WPARAM, ctypes.c_wchar_p]
    # 释放 pidl 也必须声明，否则 64 位指针会被当成 int 传，直接 OverflowError
    ole32 = ctypes.windll.ole32
    ole32.CoTaskMemFree.restype = None
    ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]

    @ctypes.WINFUNCTYPE(ctypes.c_int, wintypes.HWND, wintypes.UINT,
                        wintypes.LPARAM, wintypes.LPARAM)
    def _on_event(hwnd, msg, _lparam, _data):
        if msg == BFFM_INITIALIZED and start:
            # lParam=True 表示「选中这个路径」，树本身仍是完整的
            user32.SendMessageW(hwnd, BFFM_SETSELECTIONW, 1, start)
        return 0

    display = ctypes.create_unicode_buffer(MAX_PATH_BUF)
    bi = BROWSEINFOW()
    bi.hwndOwner = _owner_hwnd()
    bi.pidlRoot = None            # None = 桌面/此电脑，可自由向上翻
    bi.pszDisplayName = ctypes.cast(display, wintypes.LPWSTR)
    bi.lpszTitle = title
    bi.ulFlags = BIF_RETURNONLYFSDIRS | BIF_NEWDIALOGSTYLE | BIF_EDITBOX
    bi.lpfn = ctypes.cast(_on_event, ctypes.c_void_p)

    # _on_event 必须活到对话框关闭，否则回调指针指向已回收的闭包
    pidl = shell32.SHBrowseForFolderW(ctypes.byref(bi))
    del _on_event
    if not pidl:
        return ""

    path = ctypes.create_unicode_buffer(MAX_PATH_BUF)
    try:
        ok = shell32.SHGetPathFromIDListW(pidl, path)
    finally:
        ole32.CoTaskMemFree(pidl)
    return os.path.abspath(path.value) if ok and path.value else ""


# ---------- 现代文件夹选择框（IFileOpenDialog + FOS_PICKFOLDERS）----------
COINIT_APARTMENTTHREADED = 0x2
CLSCTX_INPROC_SERVER = 0x1
RPC_E_CHANGED_MODE = -2147417850        # 0x80010106，线程已是 MTA

FOS_PICKFOLDERS = 0x00000020
FOS_FORCEFILESYSTEM = 0x00000040
FOS_PATHMUSTEXIST = 0x00000800
FOS_NOCHANGEDIR = 0x00000008

SIGDN_FILESYSPATH = 0x80058000

CLSID_FILEOPENDIALOG = "{DC1C5A9C-E88A-4DDE-A5A1-60F82A20AEF7}"
IID_IFILEOPENDIALOG = "{D57C7288-D4AD-4768-BE02-9D969532D960}"
IID_ISHELLITEM = "{43826D1E-E718-42EE-BC55-A1E261C37BFE}"


class GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD),
                ("Data2", wintypes.WORD),
                ("Data3", wintypes.WORD),
                ("Data4", ctypes.c_ubyte * 8)]


def _guid(text: str) -> GUID:
    g = GUID()
    if ctypes.windll.ole32.CLSIDFromString(
            ctypes.c_wchar_p(text), ctypes.byref(g)) != 0:
        raise OSError(f"无效的 GUID：{text}")
    return g


def _com_call(obj, index: int, restype, argtypes, *args):
    """按 vtable 下标调 COM 方法。

    ctypes 没有现成的双接口绑定，只能自己掀虚表：obj 的首字段就是 lpVtbl。
    index 从 0 起算（IUnknown 占 0/1/2）。
    """
    vtbl = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    proto = ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)
    return proto(vtbl[index])(obj, *args)


def _release(obj) -> None:
    if not obj:
        return
    try:
        _com_call(obj, 2, ctypes.c_ulong, [])
    except Exception:  # noqa: BLE001
        pass


def _pick_folder_modern(title: str, start: str) -> str | None:
    """IFileOpenDialog 选文件夹。None = 这套接口起不来（需退老框）；"" = 用户取消。"""
    ole32 = ctypes.windll.ole32
    ole32.CoInitializeEx.restype = ctypes.c_long
    ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, wintypes.DWORD]
    ole32.CoUninitialize.restype = None

    # 模态对话框要 STA；线程已被别处初始化成 MTA 就趁早退，别硬弹
    hr = ole32.CoInitializeEx(None, COINIT_APARTMENTTHREADED)
    if hr not in (0, 1):
        return None
    try:
        return _pick_folder_impl(title, start)
    finally:
        ole32.CoUninitialize()      # 每次成功初始化都要配对，线程是复用的


def _pick_folder_impl(title: str, start: str) -> str | None:
    ole32 = ctypes.windll.ole32
    shell32 = ctypes.windll.shell32
    ole32.CLSIDFromString.restype = ctypes.c_long
    ole32.CLSIDFromString.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(GUID)]
    ole32.CoCreateInstance.restype = ctypes.c_long
    ole32.CoCreateInstance.argtypes = [ctypes.POINTER(GUID), ctypes.c_void_p,
                                       wintypes.DWORD, ctypes.POINTER(GUID),
                                       ctypes.POINTER(ctypes.c_void_p)]
    ole32.CoTaskMemFree.restype = None
    ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
    shell32.SHCreateItemFromParsingName.restype = ctypes.c_long
    shell32.SHCreateItemFromParsingName.argtypes = [
        ctypes.c_wchar_p, ctypes.c_void_p, ctypes.POINTER(GUID),
        ctypes.POINTER(ctypes.c_void_p)]

    clsid = _guid(CLSID_FILEOPENDIALOG)
    iid = _guid(IID_IFILEOPENDIALOG)
    dlg = ctypes.c_void_p()
    hr = ole32.CoCreateInstance(ctypes.byref(clsid), None, CLSCTX_INPROC_SERVER,
                                ctypes.byref(iid), ctypes.byref(dlg))
    if hr != 0 or not dlg:
        return None

    try:
        # vtable: 9 SetOptions / 10 GetOptions / 12 SetFolder / 17 SetTitle
        #         18 SetOkButtonLabel / 3 Show / 20 GetResult / 23 Close
        opts = wintypes.DWORD()
        if _com_call(dlg, 10, ctypes.c_long, [ctypes.POINTER(wintypes.DWORD)],
                     ctypes.byref(opts)) != 0:
            return None
        want = (opts.value | FOS_PICKFOLDERS | FOS_FORCEFILESYSTEM
                | FOS_PATHMUSTEXIST | FOS_NOCHANGEDIR)
        if _com_call(dlg, 9, ctypes.c_long, [wintypes.DWORD],
                     wintypes.DWORD(want)) != 0:
            return None
        _com_call(dlg, 17, ctypes.c_long, [ctypes.c_wchar_p], title)
        _com_call(dlg, 18, ctypes.c_long, [ctypes.c_wchar_p], "选择此文件夹")

        if start and os.path.isdir(start):
            item = ctypes.c_void_p()
            iid_item = _guid(IID_ISHELLITEM)
            if shell32.SHCreateItemFromParsingName(
                    ctypes.c_wchar_p(start), None, ctypes.byref(iid_item),
                    ctypes.byref(item)) == 0 and item:
                try:
                    _com_call(dlg, 12, ctypes.c_long, [ctypes.c_void_p], item)
                finally:
                    _release(item)

        if _com_call(dlg, 3, ctypes.c_long, [wintypes.HWND],
                     wintypes.HWND(_owner_hwnd())) != 0:
            return ""                                  # 取消

        result = ctypes.c_void_p()
        if _com_call(dlg, 20, ctypes.c_long, [ctypes.POINTER(ctypes.c_void_p)],
                     ctypes.byref(result)) != 0 or not result:
            return ""
        try:
            buf = wintypes.LPWSTR()
            if _com_call(result, 5, ctypes.c_long,
                         [ctypes.c_uint, ctypes.POINTER(wintypes.LPWSTR)],
                         ctypes.c_uint(SIGDN_FILESYSPATH),
                         ctypes.byref(buf)) != 0 or not buf:
                return ""
            try:
                return os.path.abspath(buf.value) if buf.value else ""
            finally:
                ole32.CoTaskMemFree(buf)
        finally:
            _release(result)
    finally:
        _release(dlg)


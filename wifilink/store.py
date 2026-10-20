"""传输会话的共享状态：文件清单、文本剪贴板、版本号。

设计取舍：局域网场景下文件数量通常在几十个量级，
所以 /api/state 直接返回全量快照 + 一个自增 rev，
前端发现 rev 变化才重绘。比维护增量事件流简单得多，也更不容易出错。
"""
from __future__ import annotations

import os
import secrets
import tempfile
import threading
import time
from dataclasses import dataclass, field

from . import config

# 默认接收目录（用户没配置时用）；实际生效的目录见 store.download_dir
DEFAULT_DOWNLOAD_DIR = config.default_download_dir()
# 本机拖拽/网页选择上传的暂存区（真正零拷贝的「选择文件」不经过这里）
STAGING_DIR = os.path.join(tempfile.gettempdir(), "wifilink_staging")


def _human_size(n: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    size = float(n)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{n} B"


@dataclass
class FileItem:
    id: str
    name: str
    path: str
    size: int
    origin: str          # "pc" 电脑共享出去 / "phone" 手机传上来
    created: float
    temporary: bool = False   # 临时副本（可安全删除）
    downloads: int = 0
    missing: bool = False

    def to_dict(self) -> dict:
        data = {
            "id": self.id,
            "name": self.name,
            "size": self.size,
            "sizeText": _human_size(self.size),
            "origin": self.origin,
            "created": self.created,
            "downloads": self.downloads,
            "missing": self.missing,
            "ext": (os.path.splitext(self.name)[1].lstrip(".") or "").lower(),
        }
        return data


@dataclass
class TextItem:
    id: str
    content: str
    origin: str
    created: float

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "content": self.content,
            "origin": self.origin,
            "created": self.created,
        }


class Store:
    def __init__(self, download_dir: str | None = None) -> None:
        self._lock = threading.RLock()
        self._files: dict[str, FileItem] = {}
        self._texts: list[TextItem] = []
        self._rev = 1
        self.phone_seen_at: float = 0.0
        # 启动时读一次配置：目录来源、以及「配置了但不可用」的原因
        if download_dir:
            self._download_dir = download_dir
            self._dir_source = "param"
            self._dir_error = ""
        else:
            path, source, error = config.read_download_dir()
            self._download_dir = path
            self._dir_source = source
            self._dir_error = error

    # ---------- 接收目录 ----------
    @property
    def download_dir(self) -> str:
        return self._download_dir

    @property
    def dir_source(self) -> str:
        """config / default / param，只用于展示。"""
        return self._dir_source

    @property
    def dir_error(self) -> str:
        return self._dir_error

    def set_download_dir(self, path: str, source: str = "config",
                         error: str = "") -> None:
        with self._lock:
            self._download_dir = path
            self._dir_source = source
            self._dir_error = error
            self._bump()

    # ---------- 内部 ----------
    def _bump(self) -> None:
        self._rev += 1

    def _unique_name(self, directory: str, name: str) -> str:
        """同名文件自动加 (1)(2)…，绝不覆盖用户已有文件。"""
        base, ext = os.path.splitext(name)
        candidate = name
        n = 1
        while os.path.exists(os.path.join(directory, candidate)):
            candidate = f"{base} ({n}){ext}"
            n += 1
        return candidate

    # ---------- 文件 ----------
    def add_file(self, path: str, origin: str, name: str | None = None,
                 temporary: bool = False) -> FileItem | None:
        path = os.path.abspath(path)
        if not os.path.isfile(path):
            return None
        with self._lock:
            # 电脑端重复添加同一路径：直接返回已有记录
            if origin == "pc":
                for item in self._files.values():
                    if item.origin == "pc" and item.path == path:
                        return item
            try:
                size = os.path.getsize(path)
            except OSError:
                size = 0
            item = FileItem(
                id=secrets.token_hex(6),
                name=name or os.path.basename(path),
                path=path,
                size=size,
                origin=origin,
                created=time.time(),
                temporary=temporary,
            )
            self._files[item.id] = item
            self._bump()
            return item

    def prepare_upload(self, filename: str, staging: bool = False) -> tuple[str, str]:
        """为上传分配最终落盘路径，返回 (最终名, 绝对路径)。"""
        directory = STAGING_DIR if staging else self.download_dir
        os.makedirs(directory, exist_ok=True)
        safe = os.path.basename(filename.replace("\\", "/")) or "unnamed"
        safe = safe.replace("\x00", "").strip() or "unnamed"
        with self._lock:
            final = self._unique_name(directory, safe)
        return final, os.path.join(directory, final)

    def get(self, fid: str) -> FileItem | None:
        with self._lock:
            return self._files.get(fid)

    def touch_download(self, fid: str) -> None:
        with self._lock:
            item = self._files.get(fid)
            if item:
                item.downloads += 1
                self._bump()

    def remove(self, fid: str) -> bool:
        with self._lock:
            item = self._files.pop(fid, None)
            if item is None:
                return False
            if item.temporary:
                try:
                    os.remove(item.path)
                except OSError:
                    pass
            self._bump()
            return True

    def clear(self, origin: str | None = None) -> int:
        with self._lock:
            victims = [f for f in self._files.values()
                       if origin is None or f.origin == origin]
            for item in victims:
                self._files.pop(item.id, None)
                if item.temporary:
                    try:
                        os.remove(item.path)
                    except OSError:
                        pass
            if victims:
                self._bump()
            return len(victims)

    # ---------- 文本 ----------
    def add_text(self, content: str, origin: str, limit: int = 200) -> TextItem:
        with self._lock:
            item = TextItem(
                id=secrets.token_hex(6),
                content=content,
                origin=origin,
                created=time.time(),
            )
            self._texts.insert(0, item)
            del self._texts[limit:]
            self._bump()
            return item

    def clear_texts(self) -> None:
        with self._lock:
            self._texts.clear()
            self._bump()

    def touch_phone(self) -> None:
        with self._lock:
            self.phone_seen_at = time.time()

    # ---------- 快照 ----------
    def snapshot(self, since: float = 0) -> dict:
        with self._lock:
            files = sorted(self._files.values(), key=lambda f: f.created, reverse=True)
            for item in files:
                item.missing = not os.path.exists(item.path)
            return {
                "rev": self._rev,
                "files": [f.to_dict() for f in files],
                "texts": [t.to_dict() for t in self._texts],
                "phoneOnline": (time.time() - self.phone_seen_at) < 8
                               if self.phone_seen_at else False,
                "downloadDir": self._download_dir,
                "downloadDirSource": self._dir_source,
                "downloadDirError": self._dir_error,
            }


store = Store()

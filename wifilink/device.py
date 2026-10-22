"""本机在局域网里的身份：一个稳定的随机 ID，加一个给人看的名字。

为什么要把 ID 持久化：配对白名单是按 ID 认人的。每次启动换一个 ID，
昨天配过的电脑今天又得重新确认一遍。ID 存在 `%APPDATA%\\WIFI直连\\device.json`，
和配置放一起。

ID 不是密码——它只是「这台机器的一个标签」，发现报文里谁都能看到。
真正让白名单生效的是 `pairing` 里那份从不下发的 peer_secret 算出的 HMAC。
"""
from __future__ import annotations

import json
import os
import platform
import re
import secrets
import threading

from . import config

DEVICE_FILENAME = "device.json"
MAX_NAME = 24

_lock = threading.RLock()
_cache: dict | None = None


def _path() -> str:
    return config.data_path(DEVICE_FILENAME)


def _default_name() -> str:
    host = (platform.node() or "").strip()
    host = re.sub(r"\s+", " ", host)
    return (host[:MAX_NAME] or "未命名电脑")


def _clean_name(name: str) -> str:
    name = re.sub(r"\s+", " ", (name or "").strip())
    # 名字会出现在对方的界面上，控制字符一律清掉
    name = re.sub(r"[\x00-\x1f\x7f]", "", name)
    return name[:MAX_NAME]


def _load() -> dict:
    global _cache
    with _lock:
        if _cache is not None:
            return _cache
        data: dict = {}
        try:
            with open(_path(), "r", encoding="utf-8") as fh:
                raw = json.load(fh)
            if isinstance(raw, dict):
                data = raw
        except (OSError, ValueError):
            data = {}

        changed = False
        did = data.get("id")
        if not isinstance(did, str) or not re.fullmatch(r"[0-9a-f]{32}", did):
            did = secrets.token_hex(16)
            data["id"] = did
            changed = True
        name = data.get("name")
        if not isinstance(name, str) or not name.strip():
            data["name"] = _default_name()
            changed = True
        if changed:
            _save_raw(data)
        _cache = data
        return data


def _save_raw(data: dict) -> None:
    path = _path()
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        os.replace(tmp, path)
    except OSError:
        # 写不了就算了：这次运行的 ID 只在内存里，配对会失效，但不该让程序起不来
        try:
            os.remove(tmp)
        except OSError:
            pass


def device_id() -> str:
    return _load()["id"]


def display_name() -> str:
    return _load()["name"]


def info() -> dict:
    data = _load()
    return {"id": data["id"], "name": data["name"]}


def set_name(name: str) -> tuple[bool, str, str]:
    """改名。名字只是展示用，空名直接拒绝，不做自动编号。"""
    cleaned = _clean_name(name)
    if not cleaned:
        return False, "名字不能为空", ""
    with _lock:
        data = dict(_load())
        data["name"] = cleaned
        _save_raw(data)
        global _cache
        _cache = data
    return True, "", cleaned

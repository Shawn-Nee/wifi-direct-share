"""用户配置：目前只有一项「接收目录」。

设计取舍：
- 配置放在 `%APPDATA%\\WIFI直连\\config.json`，不放 exe 旁边。
  exe 是单文件便携程序，用户可能把它丢进 Program Files 或只读介质，
  写配置会失败；APPDATA 一定可写，而且卸载 exe 不会连带丢配置。
- 目录只在内存里放一份（`store.download_dir`），启动时读一次，
  运行时改配置由接口改这一份并立即生效，不需要重启。
- 校验只管「能不能用」：路径合法、能创建、能写。
  不合法时不让它生效，把原因原样抛给调用方去显示，绝不悄悄吞掉。
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import threading

APP_NAME = "WIFI直连"
CONFIG_FILENAME = "config.json"
KEY_DOWNLOAD_DIR = "download_dir"

_ILLEGAL = re.compile(r'[<>:"|?*\x00-\x1f]')
_lock = threading.RLock()


# ---------- 路径 ----------
def default_download_dir() -> str:
    """没配置时的默认接收目录（和旧版本保持一致，老用户无感）。"""
    return os.path.join(os.path.expanduser("~"), "Downloads", APP_NAME)


def config_dir() -> str:
    base = os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, APP_NAME)


def config_path() -> str:
    # 允许用环境变量指到别处，主要方便自动化测试
    override = os.environ.get("WIFILINK_CONFIG")
    if override:
        return os.path.abspath(override)
    return os.path.join(config_dir(), CONFIG_FILENAME)


# ---------- 读 ----------
def _load_raw() -> dict:
    path = config_path()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        # 文件坏了就当没配置，不能让一个坏 JSON 把程序卡死
        return {}


def read_download_dir() -> tuple[str, str, str]:
    """读取生效的接收目录。

    返回 (目录, 来源, 错误说明)。来源为 "config" / "default"；
    配置了但不可用时，目录回退到默认值，错误说明里给出原因。
    """
    raw = _load_raw()
    wanted = raw.get(KEY_DOWNLOAD_DIR)
    if not isinstance(wanted, str) or not wanted.strip():
        return default_download_dir(), "default", ""
    ok, error, resolved = validate_dir(wanted)
    if not ok:
        return default_download_dir(), "default", f"配置的接收目录不可用（{error}），已改用默认目录"
    return resolved, "config", ""


# ---------- 校验 ----------
def normalize(path: str) -> str:
    """去掉用户手抄路径时常见的噪音：引号、首尾空格、环境变量、正斜杠。

    注意：**不做 abspath**。相对路径要能原样留下来，让校验环节明确报「请填绝对路径」，
    否则会被悄悄补成「程序当前目录 + 相对路径」，用户以为生效了其实存到了别处。
    """
    p = (path or "").strip().strip('"').strip("'").strip()
    p = os.path.expandvars(p)
    p = os.path.expanduser(p)
    if p and re.match(r"^[A-Za-z]:[^\\/]", p):      # C:foo（盘符相对）→ C:\foo
        p = p[:2] + os.sep + p[2:]
    if p:
        p = os.path.normpath(p)
    return p


def validate_dir(path: str, create: bool = True) -> tuple[bool, str, str]:
    """校验并（可选）创建目录，返回 (是否可用, 错误说明, 规范化后的路径)。"""
    p = normalize(path)
    if not p:
        return False, "路径为空", ""
    if not os.path.isabs(p):
        return False, "请填绝对路径，例如 D:\\手机上来的文件", p

    # 盘符后面的部分不能含 Windows 非法字符（盘符自身的冒号要排除）
    tail = p[2:] if re.match(r"^[A-Za-z]:", p) else p
    hit = _ILLEGAL.search(tail)
    if hit:
        return False, f"路径里有非法字符「{hit.group()}」", p

    if os.path.exists(p):
        if not os.path.isdir(p):
            return False, "这个路径是一个文件，不是文件夹", p
    elif create:
        try:
            os.makedirs(p, exist_ok=True)
        except OSError as exc:
            return False, f"目录不存在且无法创建（{exc.strerror or exc}）", p
    else:
        return False, "目录不存在", p

    # 能建不等于能写：只读目录、被占用的盘符都要在这里拦住
    probe = None
    try:
        fd, probe = tempfile.mkstemp(prefix=".wl_write_", dir=p)
        os.close(fd)
    except OSError as exc:
        return False, f"目录不可写（{exc.strerror or exc}）", p
    finally:
        if probe:
            try:
                os.remove(probe)
            except OSError:
                pass

    return True, "", p


# ---------- 写 ----------
def _atomic_write(data: dict) -> None:
    path = config_path()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".cfg_", dir=os.path.dirname(path) or ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        os.replace(tmp, path)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def save_download_dir(path: str) -> tuple[bool, str, str]:
    """校验 → 落盘 → 返回 (成功, 错误说明, 规范化后的路径)。不负责改内存里的值。"""
    ok, error, resolved = validate_dir(path)
    if not ok:
        return False, error, ""
    with _lock:
        data = _load_raw()
        data[KEY_DOWNLOAD_DIR] = resolved
        data.setdefault("version", 1)
        try:
            _atomic_write(data)
        except OSError as exc:
            return False, f"配置写入失败（{exc.strerror or exc}）", ""
    return True, "", resolved


def reset_download_dir() -> tuple[bool, str, str]:
    """删掉配置项，回到默认目录。"""
    with _lock:
        data = _load_raw()
        data.pop(KEY_DOWNLOAD_DIR, None)
        try:
            _atomic_write(data)
        except OSError as exc:
            return False, f"配置写入失败（{exc.strerror or exc}）", ""
    fallback = default_download_dir()
    ok, error, resolved = validate_dir(fallback)
    if not ok:
        return False, f"默认目录不可用（{error}）", fallback
    return True, "", resolved

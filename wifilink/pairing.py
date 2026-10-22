"""配对：一次性票据、长期密钥、白名单。

安全模型（为什么不能把访问令牌广播出去）

现在的会话令牌等于全部权限。发现报文是广播的，局域网里任何设备都能收到，
所以报文里只放「我是谁、在哪个地址」，绝不掺任何凭证。真正建立信任要走一遍：

1. B 请求配对，只报自己的设备 ID 和名字；
2. A 的主人在自己屏幕上点「允许」；
3. A 生成 peer_secret（32 字节随机，只发给 B，自己留一份）和 ticket
   （一次性，60 秒有效）；
4. B 拿 ticket 换会话 cookie，同时把 secret 存进自己那边；
5. 以后 B 再来，用 secret 对 (设备 ID, 时间戳) 算 HMAC 作为 proof，
   A 校验通过就直接发票据，不再打扰主人——这就是「记住设备」；
6. A 删掉白名单条目，secret 校验立刻失败，退回第 1 步重新确认。

为什么是 HMAC 而不是「报个 ID 就放行」

设备 ID 在发现报文里是明文广播的，谁都能抄。只有 secret 是私下传的，
抄不到 secret 就算不出 proof，「记住设备」才真的等于记住那台设备。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import threading
import time

from . import config

PEERS_FILENAME = "peers.json"
TICKET_TTL = 60          # 票据有效期（秒）
PROOF_WINDOW = 120       # proof 时间戳允许的时钟偏差
PENDING_TTL = 180        # 没人理的配对请求多久后自己消失

_lock = threading.RLock()
_cache: dict | None = None
_tickets: dict[str, dict] = {}     # ticket -> {id, exp}
_pending: dict[str, dict] = {}     # rid -> {...}


# ---------- 白名单落盘 ----------
def _path() -> str:
    return config.data_path(PEERS_FILENAME)


def _load() -> dict:
    global _cache
    with _lock:
        if _cache is not None:
            return _cache
        data: dict = {}
        try:
            with open(_path(), "r", encoding="utf-8") as fh:
                raw = json.load(fh)
            if isinstance(raw, dict) and isinstance(raw.get("peers"), dict):
                data = raw
        except (OSError, ValueError):
            data = {}
        data.setdefault("version", 1)
        data.setdefault("peers", {})
        _cache = data
        return data


def _save(data: dict) -> None:
    path = _path()
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        os.replace(tmp, path)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass


def _public(entry: dict) -> dict:
    """对外展示用：secret 永远不出现。"""
    return {
        "id": entry.get("id", ""),
        "name": entry.get("name", ""),
        "addedAt": int(entry.get("added_at", 0)),
        "lastSeen": int(entry.get("last_seen", 0)),
        "lastIp": entry.get("last_ip", ""),
    }


# ---------- 白名单 ----------
def list_peers() -> list[dict]:
    data = _load()
    items = [_public(dict(v, id=k)) for k, v in data["peers"].items()]
    items.sort(key=lambda x: x["lastSeen"], reverse=True)
    return items


def get_peer(device_id: str) -> dict | None:
    entry = _load()["peers"].get(device_id)
    return dict(entry, id=device_id) if entry else None


def add_peer(device_id: str, name: str, ip: str, secret: str) -> None:
    """写入/更新一条白名单。secret 由调用方决定是「我验他」还是「我给他」的。"""
    with _lock:
        data = _load()
        old = data["peers"].get(device_id, {})
        data["peers"][device_id] = {
            "name": name or old.get("name", ""),
            "secret": secret,
            "added_at": old.get("added_at", int(time.time())),
            "last_seen": int(time.time()),
            "last_ip": ip or old.get("last_ip", ""),
        }
        _save(data)


def touch(device_id: str, ip: str = "") -> None:
    with _lock:
        data = _load()
        entry = data["peers"].get(device_id)
        if not entry:
            return
        entry["last_seen"] = int(time.time())
        if ip:
            entry["last_ip"] = ip
        _save(data)


def remove_peer(device_id: str) -> bool:
    with _lock:
        data = _load()
        if device_id not in data["peers"]:
            return False
        data["peers"].pop(device_id, None)
        _save(data)
    return True


# ---------- proof ----------
def make_proof(secret: str, device_id: str, ts: int | None = None) -> tuple[int, str]:
    """我这边（被信任的一方）在重连时证明「我确实拿着当初那条 secret」。"""
    ts = int(ts if ts is not None else time.time())
    msg = f"{device_id}:{ts}".encode()
    return ts, hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()


def verify_proof(device_id: str, ts: int, proof: str) -> bool:
    entry = get_peer(device_id)
    if not entry:
        return False
    try:
        ts = int(ts)
    except (TypeError, ValueError):
        return False
    if abs(time.time() - ts) > PROOF_WINDOW:
        return False
    secret = entry.get("secret") or ""
    if not secret or not isinstance(proof, str):
        return False
    expect = hmac.new(secret.encode(), f"{device_id}:{ts}".encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expect, proof.strip().lower())


# ---------- 票据 ----------
def issue_ticket(device_id: str) -> str:
    ticket = secrets.token_urlsafe(24)
    now = time.time()
    with _lock:
        for key, val in list(_tickets.items()):
            if val["exp"] < now:
                _tickets.pop(key, None)
        _tickets[ticket] = {"id": device_id, "exp": now + TICKET_TTL}
    return ticket


def redeem_ticket(ticket: str) -> str | None:
    """票据换设备 ID。有效期内可重复兑换（浏览器可能重试），过期即失效。"""
    if not ticket:
        return None
    with _lock:
        entry = _tickets.get(ticket)
        if not entry:
            return None
        if entry["exp"] < time.time():
            _tickets.pop(ticket, None)
            return None
        return entry["id"]


def revoke_tickets(device_id: str) -> None:
    with _lock:
        for key, val in list(_tickets.items()):
            if val["id"] == device_id:
                _tickets.pop(key, None)


# ---------- 待确认的配对请求 ----------
def pending_create(device_id: str, name: str, ip: str) -> dict:
    now = time.time()
    with _lock:
        for rid, item in list(_pending.items()):
            if now - item["ts"] > PENDING_TTL:
                _pending.pop(rid, None)
        # 同一台设备重复点「连接」不该堆出一串弹窗
        for rid, item in _pending.items():
            if item["id"] == device_id and item["state"] == "pending":
                item["ts"] = now
                item["ip"] = ip
                item["name"] = name
                return dict(item)
        rid = secrets.token_hex(6)
        item = {"rid": rid, "id": device_id, "name": name, "ip": ip,
                "ts": now, "state": "pending", "secret": "", "ticket": ""}
        _pending[rid] = item
        return dict(item)


def pending_list() -> list[dict]:
    now = time.time()
    with _lock:
        out = []
        for item in _pending.values():
            if item["state"] == "pending" and now - item["ts"] <= PENDING_TTL:
                out.append({"rid": item["rid"], "id": item["id"],
                            "name": item["name"], "ip": item["ip"],
                            "ts": int(item["ts"])})
    out.sort(key=lambda x: x["ts"])
    return out


def pending_get(rid: str) -> dict | None:
    with _lock:
        item = _pending.get(rid)
        return dict(item) if item else None


def pending_consume(rid: str) -> dict | None:
    """取请求详情，顺手把 secret 清空——长期密钥只发给对方一次。

    之后想再用，只能靠这张票据（一次性、60 秒）；票据也没了就得重新确认。
    比把密钥一直留在内存里等人来读安全得多。
    """
    with _lock:
        item = _pending.get(rid)
        if not item:
            return None
        out = dict(item)
        item["secret"] = ""
        return out


def pending_settle(rid: str, allow: bool) -> dict | None:
    """主人点了允许或拒绝。允许时顺带生成 secret 与票据。"""
    with _lock:
        item = _pending.get(rid)
        if not item or item["state"] != "pending":
            return None
        if allow:
            secret = secrets.token_hex(32)
            add_peer(item["id"], item["name"], item["ip"], secret)
            item["secret"] = secret
            item["ticket"] = issue_ticket(item["id"])
            item["state"] = "allowed"
        else:
            item["state"] = "denied"
        item["settled_at"] = time.time()
        return dict(item)


def pending_drop(rid: str) -> None:
    with _lock:
        _pending.pop(rid, None)

"""局域网 IPv4 地址探测。

目标：在有多块网卡（VPN / 虚拟机 / WSL / Hyper-V）的机器上，
尽可能挑出「手机能连上的那个」地址作为默认推荐值。
"""
from __future__ import annotations

import ipaddress
import socket


def _default_route_ip() -> str | None:
    """通过 UDP connect 取默认出口网卡的 IP（不会真的发包）。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.settimeout(0.3)
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


def _enumerated_ips() -> list[str]:
    ips: list[str] = []
    try:
        infos = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET, socket.SOCK_STREAM)
    except OSError:
        infos = []
    for info in infos:
        ip = info[4][0]
        if ip not in ips:
            ips.append(ip)
    return ips


def _usable(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if addr.is_loopback or addr.is_link_local or addr.is_multicast:
        return False
    return addr.is_private


def _rank(ip: str) -> tuple:
    """排序权重：192.168.x 最像家用路由，其次 10.x，再次 172.16-31.x。"""
    if ip.startswith("192.168."):
        return (0, ip)
    if ip.startswith("10."):
        return (1, ip)
    return (2, ip)


def local_ips() -> list[str]:
    """返回可用的局域网 IPv4 列表，最可能正确的一项排在最前。"""
    primary = _default_route_ip()
    candidates = [ip for ip in _enumerated_ips() if _usable(ip)]

    if primary and _usable(primary) and primary not in candidates:
        candidates.insert(0, primary)

    candidates.sort(key=_rank)

    # 把默认出口 IP 提到第一位（如果它在候选里）
    if primary and primary in candidates:
        candidates.remove(primary)
        candidates.insert(0, primary)

    return candidates or []


def primary_ip() -> str:
    ips = local_ips()
    if ips:
        return ips[0]
    # 极端情况：完全没网，绑定回环，至少本机能用
    return "127.0.0.1"

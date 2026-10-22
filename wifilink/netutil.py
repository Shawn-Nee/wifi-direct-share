"""局域网 IPv4 地址探测。

目标：在有多块网卡（VPN / 虚拟机 / WSL / Hyper-V）的机器上，
挑出「手机真能连上」的那个地址作为默认推荐值。

早先的做法是 `UDP connect 8.8.8.8 → getsockname()`，取默认出口地址。
这在装了 VPN / 代理 TUN 的机器上会直接选错：TUN 网卡抢走默认路由后，
出口地址变成 172.19.0.x 这种隧道地址，手机在同一 Wi-Fi 下也连不上。

现在的做法是走 Windows IP Helper（GetAdaptersAddresses）把每块网卡的真实
属性拿全（介质类型、运行状态、网关、友好名、描述），再打分排序：
真实物理网卡 > 有网关 > 无线/有线介质 > 192.168 段；
VPN / 虚拟机 / WSL / 蓝牙这类虚拟网卡一律重罚，除非实在没有别的选择。
"""
from __future__ import annotations

import ctypes
import ipaddress
import socket
from ctypes import wintypes

AF_UNSPEC = 0
GAA_FLAG_SKIP_ANYCAST = 0x0002
GAA_FLAG_SKIP_MULTICAST = 0x0004
GAA_FLAG_SKIP_DNS_SERVER = 0x0008
GAA_FLAG_INCLUDE_GATEWAYS = 0x0080
ERROR_BUFFER_OVERFLOW = 111

IF_OPER_STATUS_UP = 1
IF_TYPE_ETHERNET = 6          # 有线以太网
IF_TYPE_PPP = 23              # 拨号 / 部分 VPN
IF_TYPE_SOFTWARE_LOOPBACK = 24
IF_TYPE_IEEE80211 = 71        # Wi-Fi
IF_TYPE_TUNNEL = 131          # 各种隧道（VPN、WSL 的某些实现）

# 网卡名 / 描述里出现这些词，基本可以断定不是家里那个 Wi-Fi
_VIRTUAL_WORDS = (
    "vethernet", "wsl", "hyper-v", "hyperv", "docker",
    "vmware", "vmnet", "virtualbox", "vbox", "virtual",
    "tap-windows", "tap adapter", "openvpn", "wireguard", "wintun",
    "tailscale", "zerotier", "radmin", "hamachi", "npcap",
    "tunnel", "vpn", "safelink", "clash", "sing-box", "singbox",
    "bluetooth", "loopback", "npcap loopback",
)


class _SOCKET_ADDRESS(ctypes.Structure):
    _fields_ = [("lpSockaddr", ctypes.c_void_p), ("iSockaddrLength", ctypes.c_int)]


class _SOCKADDR_IN(ctypes.Structure):
    _fields_ = [
        ("sin_family", wintypes.USHORT),
        ("sin_port", wintypes.USHORT),
        ("sin_addr", wintypes.ULONG),
        ("sin_zero", ctypes.c_ubyte * 8),
    ]


class _IP_ADAPTER_UNICAST_ADDRESS(ctypes.Structure):
    pass


_IP_ADAPTER_UNICAST_ADDRESS._fields_ = [
    ("Length", wintypes.ULONG),
    ("Flags", wintypes.DWORD),
    ("Next", ctypes.POINTER(_IP_ADAPTER_UNICAST_ADDRESS)),
    ("Address", _SOCKET_ADDRESS),
    ("PrefixOrigin", ctypes.c_int),
    ("SuffixOrigin", ctypes.c_int),
    ("DadState", ctypes.c_int),
    ("ValidLifetime", wintypes.ULONG),
    ("PreferredLifetime", wintypes.ULONG),
    ("LeaseLifetime", wintypes.ULONG),
    ("OnLinkPrefixLength", ctypes.c_ubyte),
]


class _IP_ADAPTER_ADDRESSES(ctypes.Structure):
    pass


_IP_ADAPTER_ADDRESSES._fields_ = [
    ("Length", wintypes.ULONG),
    ("IfIndex", wintypes.DWORD),
    ("Next", ctypes.POINTER(_IP_ADAPTER_ADDRESSES)),
    ("AdapterName", ctypes.c_char_p),
    ("FirstUnicastAddress", ctypes.POINTER(_IP_ADAPTER_UNICAST_ADDRESS)),
    ("FirstAnycastAddress", ctypes.c_void_p),
    ("FirstMulticastAddress", ctypes.c_void_p),
    ("FirstDnsServerAddress", ctypes.c_void_p),
    ("DnsSuffix", ctypes.c_wchar_p),
    ("Description", ctypes.c_wchar_p),
    ("FriendlyName", ctypes.c_wchar_p),
    ("PhysicalAddress", ctypes.c_ubyte * 8),
    ("PhysicalAddressLength", wintypes.ULONG),
    ("Flags", wintypes.ULONG),
    ("Mtu", wintypes.ULONG),
    ("IfType", wintypes.ULONG),
    ("OperStatus", ctypes.c_int),
    ("Ipv6IfIndex", wintypes.ULONG),
    ("ZoneIndices", wintypes.ULONG * 16),
    ("FirstPrefix", ctypes.c_void_p),
    ("TransmitLinkSpeed", ctypes.c_ulonglong),
    ("ReceiveLinkSpeed", ctypes.c_ulonglong),
    ("FirstWinsServerAddress", ctypes.c_void_p),
    ("FirstGatewayAddress", ctypes.POINTER(_IP_ADAPTER_UNICAST_ADDRESS)),
]


class Nic:
    """一块网卡上的一个 IPv4 地址，连同判断它是不是「真网卡」需要的材料。"""

    __slots__ = ("ip", "prefix", "name", "desc", "iftype", "up", "gateway", "virtual", "score")

    def __init__(self, ip: str, prefix: int, name: str, desc: str,
                 iftype: int, up: bool, gateway: str | None):
        self.ip = ip
        self.prefix = prefix
        self.name = name or desc or "网卡"
        self.desc = desc or ""
        self.iftype = iftype
        self.up = up
        self.gateway = gateway
        self.virtual = _is_virtual(self.name, self.desc, iftype)
        self.score = 0

    def to_dict(self) -> dict:
        return {
            "ip": self.ip,
            "name": self.name,
            "gateway": self.gateway or "",
            "virtual": self.virtual,
        }

    def __repr__(self) -> str:  # 调试用
        return (f"<Nic {self.ip}/{self.prefix} {self.name!r} iftype={self.iftype} "
                f"up={self.up} gw={self.gateway} virtual={self.virtual} score={self.score}>")


def _is_virtual(name: str, desc: str, iftype: int) -> bool:
    if iftype in (IF_TYPE_TUNNEL, IF_TYPE_PPP, IF_TYPE_SOFTWARE_LOOPBACK):
        return True
    text = f"{name} {desc}".lower()
    return any(word in text for word in _VIRTUAL_WORDS)


def _sockaddr_to_ip(ptr: int, size: int) -> str | None:
    """从 SOCKET_ADDRESS 里掏出点分十进制 IPv4，非 IPv4 返回 None。"""
    if not ptr or size < ctypes.sizeof(_SOCKADDR_IN):
        return None
    sa = ctypes.cast(ptr, ctypes.POINTER(_SOCKADDR_IN)).contents
    if sa.sin_family != socket.AF_INET:
        return None
    return socket.inet_ntoa(sa.sin_addr.to_bytes(4, "little"))


def _enum_adapters() -> list[Nic]:
    """GetAdaptersAddresses 枚举全部网卡。失败（非 Windows、API 异常）返回空表。"""
    if not hasattr(ctypes, "windll"):
        return []
    try:
        iphlpapi = ctypes.windll.iphlpapi
        iphlpapi.GetAdaptersAddresses.restype = wintypes.ULONG
        iphlpapi.GetAdaptersAddresses.argtypes = [
            wintypes.ULONG, wintypes.ULONG, ctypes.c_void_p,
            ctypes.POINTER(_IP_ADAPTER_ADDRESSES), ctypes.POINTER(wintypes.ULONG),
        ]
        flags = (GAA_FLAG_SKIP_ANYCAST | GAA_FLAG_SKIP_MULTICAST
                 | GAA_FLAG_SKIP_DNS_SERVER | GAA_FLAG_INCLUDE_GATEWAYS)
        size = wintypes.ULONG(24 * 1024)
        buf = None
        for _ in range(3):
            buf = ctypes.create_string_buffer(size.value)
            ret = iphlpapi.GetAdaptersAddresses(
                AF_UNSPEC, flags, None,
                ctypes.cast(buf, ctypes.POINTER(_IP_ADAPTER_ADDRESSES)),
                ctypes.byref(size),
            )
            if ret == 0:
                break
            if ret != ERROR_BUFFER_OVERFLOW:
                return []
        else:
            return []
    except (OSError, AttributeError):
        return []

    nics: list[Nic] = []
    node = ctypes.cast(buf, ctypes.POINTER(_IP_ADAPTER_ADDRESSES))
    while node:
        a = node.contents
        name = a.FriendlyName or ""
        desc = a.Description or ""
        up = a.OperStatus == IF_OPER_STATUS_UP

        gateway = None
        gw = a.FirstGatewayAddress
        while gw:
            ip = _sockaddr_to_ip(gw.contents.Address.lpSockaddr,
                                 gw.contents.Address.iSockaddrLength)
            if ip:
                gateway = ip
                break
            gw = gw.contents.Next

        uni = a.FirstUnicastAddress
        while uni:
            u = uni.contents
            ip = _sockaddr_to_ip(u.Address.lpSockaddr, u.Address.iSockaddrLength)
            if ip:
                nics.append(Nic(ip, int(u.OnLinkPrefixLength), name, desc,
                                int(a.IfType), up, gateway))
            uni = u.Next

        node = a.Next
    return nics


def _usable(nic: Nic) -> bool:
    try:
        addr = ipaddress.ip_address(nic.ip)
    except ValueError:
        return False
    if addr.is_loopback or addr.is_link_local or addr.is_multicast:
        return False       # 169.254.x 是没拿到 DHCP 的自分配地址，等于没联网
    return addr.is_private


def _score(nic: Nic) -> int:
    """分数越高越像「手机能连上的那块网卡」。"""
    addr = ipaddress.ip_address(nic.ip)
    score = 0

    if nic.virtual:
        score -= 200
    if not nic.up:
        score -= 100
    if not nic.gateway:
        # 没有默认网关：要么是虚拟网卡，要么不在正常家庭网络里
        score -= 60

    if nic.iftype == IF_TYPE_IEEE80211:
        score += 40          # Wi-Fi
    elif nic.iftype == IF_TYPE_ETHERNET:
        score += 32          # 有线

    if nic.ip.startswith("192.168."):
        score += 20
        if nic.ip.endswith(".1"):
            # 自己占着 .1 多半是 ICS 热点或虚拟网卡，手机那边通常是 .1 的网关
            score -= 18
    elif nic.ip.startswith("10."):
        score += 10
    elif addr in ipaddress.ip_network("172.16.0.0/12"):
        score -= 12          # Docker / WSL / VPN 的重灾区

    if nic.gateway:
        try:
            same = ipaddress.ip_address(nic.gateway) in ipaddress.ip_network(
                f"{nic.ip}/{nic.prefix}", strict=False)
            if same:
                score += 10  # 网关和自己同网段，是正经局域网
        except ValueError:
            pass

    return score


def _default_route_ip() -> str | None:
    """UDP connect 取默认出口地址（不会真的发包）。仅作枚举失败时的兜底。"""
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
    try:
        infos = socket.getaddrinfo(socket.gethostname(), None,
                                   socket.AF_INET, socket.SOCK_STREAM)
    except OSError:
        return []
    ips: list[str] = []
    for info in infos:
        ip = info[4][0]
        if ip not in ips:
            ips.append(ip)
    return ips


def nics() -> list[Nic]:
    """所有可用的局域网网卡，最可能正确的排在最前。"""
    found = [n for n in _enum_adapters() if _usable(n)]
    if not found:
        # 枚举不到（老系统 / 权限 / 意外的 API 失败）就退回老办法
        candidates = [ip for ip in _enumerated_ips() if _usable_ip(ip)]
        route = _default_route_ip()
        if route and _usable_ip(route) and route not in candidates:
            candidates.insert(0, route)
        found = [Nic(ip, 24, "", "", 0, True, None) for ip in candidates]
        for n in found:
            n.virtual = False

    seen: set[str] = set()
    unique = []
    for n in found:
        if n.ip in seen:
            continue
        seen.add(n.ip)
        n.score = _score(n)
        unique.append(n)

    unique.sort(key=lambda n: (-n.score, n.ip))
    return unique


def _usable_ip(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if addr.is_loopback or addr.is_link_local or addr.is_multicast:
        return False
    return addr.is_private


def local_ips() -> list[str]:
    """返回可用的局域网 IPv4 列表，最可能正确的一项排在最前。"""
    return [n.ip for n in nics()] or []


def primary_ip() -> str:
    ips = local_ips()
    if ips:
        return ips[0]
    # 极端情况：完全没网，绑定回环，至少本机能用
    return "127.0.0.1"


def nic_of(ip: str) -> Nic | None:
    for n in nics():
        if n.ip == ip:
            return n
    return None

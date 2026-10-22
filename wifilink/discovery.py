"""局域网设备发现（UDP 广播，默认 8729 端口）。

为什么不用 mDNS / zeroconf

- 要多带一个第三方依赖，PyInstaller 还得单独处理；
- Windows 上组播要额外放行，效果和裸 UDP 广播没区别；
- 我们只需要「谁在、叫什么、HTTP 端口多少」，不需要服务类型发现那套复杂度。

为什么必须逐网卡发定向广播

开了 VPN 时默认路由落在隧道上，往 255.255.255.255 发的包很可能从隧道出去，
同一个 Wi-Fi 里的另一台电脑永远收不到。所以按每块网卡的 IP/掩码算出各自的
广播地址（192.168.43.255 这种），逐个发一遍。

报文里有什么、没有什么

只有「设备 ID / 名字 / HTTP 端口」，**不含任何凭证**。配对是另一件事，
由 pairing.py 走一次性票据，见那边的说明。

回复也用广播（而不是单播回请求方）

Windows 上了 SO_REUSEADDR 之后，同一个端口上的多个 UDP socket 里，
单播包只有最后一个绑定的那个能收到。同一台机器开两份实例调试时会互相抢包，
广播则所有 socket 都能收到。一次查询几台电脑回几个包，这点流量可以忽略。
"""
from __future__ import annotations

import ipaddress
import json
import socket
import threading
import time

from . import netutil

DISCOVERY_PORT = 8729
MAGIC = "wifi-link"
VERSION = 1
PEER_TTL = 45          # 多久没消息就当它下线
QUERY_INTERVAL = 15    # 自动搜索间隔
RECV_TIMEOUT = 1.0


def _targets() -> list[tuple[str, str]]:
    """(本机网卡地址, 该网卡的广播地址)，外加全局广播兜底。

    带上源地址是有原因的：一台机器可能在好几块网卡上各回一次广播
    （Wi-Fi 一次、VMware 一次、VPN 一次），接收方会看到同一个设备
    有多个来源 IP。报文里标出哪个是「这台机器推荐的地址」，
    对端就不会被 192.168.96.1 这种虚拟网卡的地址骗走。
    """
    out: list[tuple[str, str]] = []
    for nic in netutil.nics():
        prefix = nic.prefix if 8 <= (nic.prefix or 0) <= 30 else 24
        try:
            net = ipaddress.ip_network(f"{nic.ip}/{prefix}", strict=False)
        except ValueError:
            continue
        item = (nic.ip, str(net.broadcast_address))
        if item not in out:
            out.append(item)
    out.append(("", "255.255.255.255"))
    return out


class Discovery:
    """一个后台线程：定时广播查询，同时收别人的广播。

    失败不影响主功能——发现不了就让用户手动连，程序照常传文件。
    """

    def __init__(self, http_port: int, device_id: str, name: str,
                 udp_port: int = DISCOVERY_PORT) -> None:
        self.http_port = http_port
        self.device_id = device_id
        self.name = name
        self.udp_port = udp_port
        self.error = ""
        # 推荐地址（去掉 VPN / 虚拟机之后最像「手机能连上」的那个）
        self.primary_ip = netutil.primary_ip()
        self._peers: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None

    # ---------- 生命周期 ----------
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="wifilink-discovery")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        sock = self._sock
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

    def _run(self) -> None:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.bind(("", self.udp_port))
            sock.settimeout(RECV_TIMEOUT)
        except OSError as exc:
            self.error = f"发现服务启动失败（UDP {self.udp_port}）：{exc}"
            return
        self._sock = sock
        last_query = 0.0
        while not self._stop.is_set():
            now = time.time()
            if now - last_query >= QUERY_INTERVAL:
                self.query()
                last_query = now
            try:
                data, addr = sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            self._handle(data, addr)

    # ---------- 收发 ----------
    def _send(self, kind: str) -> None:
        sock = self._sock
        if sock is None:
            return
        for src, addr in _targets():
            payload = {"wl": MAGIC, "v": VERSION, "kind": kind,
                       "id": self.device_id, "name": self.name,
                       "port": self.http_port,
                       "nic": src,
                       "primary": bool(src) and src == self.primary_ip}
            try:
                sock.sendto(json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                            (addr, self.udp_port))
            except OSError:
                continue

    def query(self) -> None:
        """广播一句「谁在线上」。界面上点「重新搜索」也走这里。"""
        self._send("query")

    def _reply(self) -> None:
        self._send("reply")

    def _handle(self, data: bytes, addr: tuple) -> None:
        try:
            msg = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return
        if not isinstance(msg, dict) or msg.get("wl") != MAGIC:
            return
        # 只处理同一个大版本，避免以后改协议时新旧版本互相误解
        if msg.get("v") != VERSION:
            return
        pid = msg.get("id")
        if not isinstance(pid, str) or pid == self.device_id:
            return                       # 自己发的，忽略
        kind = msg.get("kind")
        if kind == "query":
            self._reply()
            return
        if kind != "reply":
            return

        port = msg.get("port")
        if not isinstance(port, int) or not (0 < port < 65536):
            return
        name = msg.get("name")
        if not isinstance(name, str):
            name = ""
        ip = addr[0] if addr else ""
        is_primary = bool(msg.get("primary"))
        with self._lock:
            old = self._peers.get(pid)
            # 同一台机器会从好几块网卡各回一次。已经拿到「推荐地址」之后，
            # 就不让 VMware / VPN 的回包把它覆盖成手机连不上的地址。
            if old and old.get("primary") and not is_primary:
                return
            self._peers[pid] = {
                "id": pid,
                "name": name or (old or {}).get("name", "") or ip,
                "ip": ip,
                "port": port,
                "primary": is_primary,
                "lastSeen": int(time.time()),
            }

    # ---------- 查询结果 ----------
    def peers(self) -> list[dict]:
        now = time.time()
        with self._lock:
            for pid, item in list(self._peers.items()):
                if now - item["lastSeen"] > PEER_TTL:
                    self._peers.pop(pid, None)
            return [dict(v) for v in self._peers.values()]

    def snapshot(self) -> dict:
        return {
            "enabled": bool(self._sock) and not self.error,
            "error": self.error,
            "port": self.udp_port,
            "peers": self.peers(),
        }

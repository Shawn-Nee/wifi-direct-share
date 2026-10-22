"""HTTP 服务：手机端页面、上传下载、桌面端控制接口。"""
from __future__ import annotations

import io
import json
import os
import platform
import re
import secrets
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import quote

import qrcode
from flask import (Flask, Response, jsonify, redirect, request, send_file,
                   send_from_directory, make_response)
from PIL import Image

from . import config, device, netutil, pairing
from .store import store


def _resource_root() -> str:
    """PyInstaller 单文件模式下资源被解包到 _MEIPASS。"""
    if getattr(sys, "frozen", False):
        return getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


WEB_DIR = os.path.join(_resource_root(), "web")
PHONE_COOKIE = "wl_token"
CHUNK = 512 * 1024


class UploadTracker:
    """记录正在进行的上传，让电脑端能实时看到手机的上传进度。"""

    def __init__(self) -> None:
        self._items: dict[str, dict] = {}
        self._lock = threading.Lock()

    def start(self, name: str, size: int) -> str:
        uid = secrets.token_hex(4)
        with self._lock:
            self._items[uid] = {"id": uid, "name": name, "size": size,
                                "done": 0, "started": time.time()}
        return uid

    def update(self, uid: str, done: int) -> None:
        with self._lock:
            if uid in self._items:
                self._items[uid]["done"] = done

    def finish(self, uid: str) -> None:
        with self._lock:
            self._items.pop(uid, None)

    def snapshot(self) -> list[dict]:
        with self._lock:
            return list(self._items.values())


uploads = UploadTracker()
_pick_lock = threading.Lock()   # 防止连点弹出多个文件框
_shot_lock = threading.Lock()   # 抓屏要最小化/恢复窗口，串行执行免得闪

# 最近一次截图的结果。框选覆盖层关得比 toast 还快，成功与否得等主窗口恢复后
# 由前端轮询时补一句提示（见 /api/state 里的 shot 字段）。
_shot_info: dict = {"seq": 0}


def _note_shot(**fields) -> None:
    seq = _shot_info["seq"] + 1
    _shot_info.clear()
    _shot_info.update(seq=seq, **fields)


def _is_local() -> bool:
    return request.remote_addr in ("127.0.0.1", "::1", "localhost")


# ---------- 图片：内联预览 + 缩略图 ----------
# 为什么不能一律用附件下载：
# 鸿蒙 5 起，图库只收录「图库目录」（DCIM/Pictures）里的文件，浏览器下载会落到
# Download/，于是图库看不到、文件管理里点「放入相册」也会被系统判为来路不明而拒绝。
# 让图片在浏览器里内联渲染，用户长按 → 保存图片，走的是浏览器自己的媒体入库通道，
# 直接进图库，这才是手机上可靠的做法。

def _sniff_image_mime(path: str) -> str | None:
    """按文件头判断真实图片类型——扩展名可能是错的，不能信。"""
    try:
        with open(path, "rb") as fh:
            head = fh.read(32)
    except OSError:
        return None
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if head.startswith(b"BM"):
        return "image/bmp"
    if head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand in (b"heic", b"heix", b"hevc", b"heim", b"heis", b"mif1", b"msf1"):
            return "image/heic"
        if brand in (b"avif", b"avis"):
            return "image/avif"
    return None


def _disposition(kind: str, name: str) -> str:
    """手写 Content-Disposition。

    werkzeug 自动生成时，纯 ASCII 回退名会把中文整个剥掉，只剩 " 2026-10-20 .png"
    这种以空格开头的怪名字，个别手机下载器就存成了半个名字。这里自己做：
    非 ASCII 回退名兜底，真正的中文名走 RFC 5987 的 filename*。
    """
    fallback = re.sub(r'[\r\n"\\]', "", name.encode("ascii", "ignore").decode())
    fallback = fallback.strip() or "file"
    return f"{kind}; filename=\"{fallback}\"; filename*=UTF-8''{quote(name)}"


_thumb_cache: dict[tuple, bytes] = {}
_thumb_lock = threading.Lock()

# 本机发起、正在等对方确认的配对请求：rid -> {base, id, ip}
_outgoing: dict[str, dict] = {}
_outgoing_lock = threading.Lock()

# 跨机配对请求的简单限流：同一来源一分钟内最多 6 次，
# 免得有人写个脚本反复敲门、把主人的屏幕刷满确认框。
_pair_hits: dict[str, list[float]] = {}
_pair_hits_lock = threading.Lock()


def _pair_rate_ok(ip: str) -> bool:
    now = time.time()
    key = ip or "?"
    with _pair_hits_lock:
        hits = [t for t in _pair_hits.get(key, []) if now - t < 60]
        if len(hits) >= 6:
            _pair_hits[key] = hits
            return False
        hits.append(now)
        _pair_hits[key] = hits
        if len(_pair_hits) > 64:            # 别让这张表无限长大
            for k, v in list(_pair_hits.items()):
                if not any(now - t < 60 for t in v):
                    _pair_hits.pop(k, None)
    return True

# 访问对端必须绕开系统代理：用户常年挂着 VPN / 代理，
# 不绕的话发往 192.168.x 的请求会被塞进代理隧道，永远连不上。
_direct = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class PeerError(RuntimeError):
    """和对端通信失败（连不上 / 返回非 JSON / 超时）。"""


def _peer_json(base: str, path: str, payload: dict | None = None,
               timeout: float = 6.0) -> dict:
    """请求另一台电脑上的本程序。返回解析后的 JSON，失败抛 PeerError。"""
    url = base.rstrip("/") + path
    headers = {"Accept": "application/json"}
    data = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    req = urllib.request.Request(url, data=data, headers=headers,
                                 method="POST" if payload is not None else "GET")
    try:
        with _direct.open(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        try:
            return json.loads(body)
        except ValueError:
            raise PeerError(f"对端返回 HTTP {exc.code}") from exc
    except (urllib.error.URLError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise PeerError(str(reason) or "连接超时") from exc
    try:
        out = json.loads(body or "{}")
    except ValueError:
        raise PeerError("对端返回的不是有效响应") from None
    return out if isinstance(out, dict) else {}


def _open_peer(url: str) -> None:
    """在默认浏览器里打开对端页面。失败不阻断流程——界面上还留着可点的链接。"""
    if os.environ.get("WIFILINK_NO_OPEN"):      # 自动化测试时别真去开浏览器
        return
    try:
        if os.name == "nt":
            os.startfile(url)       # noqa: S606  （打开默认浏览器，不是执行程序）
        else:
            import webbrowser
            webbrowser.open(url)
    except OSError:
        pass



def create_app(token: str, host_ip: str, port: int, discovery=None) -> Flask:
    app = Flask(__name__, static_folder=None)
    app.config["MAX_CONTENT_LENGTH"] = None
    share_url = f"http://{host_ip}:{port}/j/{token}"

    # ---------- 鉴权 ----------
    @app.before_request
    def _guard():
        path = request.path
        if _is_local():
            return None
        if path.startswith("/j/") or path.startswith("/p/"):
            return None                      # 扫码入口 / 配对票据入口
        if path.startswith("/static/") or path == "/favicon.ico":
            return None
        if path == "/api/discovery/pair" or path.startswith("/api/pair/status/"):
            return None                      # 跨机配对：只交换「我是谁」，不碰数据
        if path.startswith("/desktop"):
            return jsonify(ok=False, error="forbidden"), 403
        supplied = (request.cookies.get(PHONE_COOKIE)
                    or request.args.get("t")
                    or request.headers.get("X-Token"))
        if supplied != token:
            return jsonify(ok=False, error="链接已失效，请重新扫码"), 403
        store.touch_phone()
        return None

    # ---------- 页面 ----------
    @app.get("/j/<tk>")
    def join(tk: str):
        if tk != token:
            return Response("链接已失效，请在电脑上重新生成二维码。", 403,
                            mimetype="text/plain; charset=utf-8")
        resp = make_response(redirect("/"))
        resp.set_cookie(PHONE_COOKIE, token, max_age=60 * 60 * 24 * 30,
                        samesite="Lax")
        return resp

    @app.get("/p/<tk>")
    def peer_join(tk: str):
        """已配对电脑的入口。票据由对方点「允许」后下发，60 秒内有效。"""
        pid = pairing.redeem_ticket(tk)
        if not pid:
            return Response("这条连接链接已经失效，请在发起连接的电脑上重新连一次。",
                            403, mimetype="text/plain; charset=utf-8")
        pairing.touch(pid, request.remote_addr or "")
        resp = make_response(redirect("/"))
        resp.set_cookie(PHONE_COOKIE, token, max_age=60 * 60 * 24 * 30,
                        samesite="Lax")
        # 只是给页面标一句「已连接到谁」，不参与鉴权
        resp.set_cookie("wl_peer", pid, max_age=60 * 60 * 24 * 30, samesite="Lax")
        return resp

    @app.get("/")
    def mobile():
        return send_from_directory(WEB_DIR, "mobile.html")

    @app.get("/desktop")
    def desktop():
        if not _is_local():
            return Response("forbidden", 403)
        return send_from_directory(WEB_DIR, "desktop.html")

    @app.get("/shot")
    def shot_page():
        """框选覆盖层页面。由 open_shot_window 以整屏无边框窗口打开。"""
        if not _is_local():
            return Response("forbidden", 403)
        return send_from_directory(WEB_DIR, "shot.html")

    @app.get("/static/<path:rel>")
    def static_files(rel: str):
        return send_from_directory(os.path.join(WEB_DIR, "static"), rel)

    @app.get("/favicon.ico")
    def favicon():
        return Response(status=204)

    # ---------- 连接信息 ----------
    @app.get("/api/session")
    def session():
        if not _is_local():
            return jsonify(ok=False), 403
        cards = netutil.nics()
        return jsonify(
            ok=True,
            url=share_url,
            token=token,
            ip=host_ip,
            # 当前 IP 属于哪块网卡（WLAN / 以太网 / 某个 VPN），给界面做标注用
            ipName=next((c.name for c in cards if c.ip == host_ip), ""),
            port=port,
            ips=[c.ip for c in cards],
            nics=[c.to_dict() for c in cards],
            hostname=platform.node(),
            downloadDir=store.download_dir,
            downloadDirSource=store.dir_source,
            downloadDirError=store.dir_error,
            defaultDownloadDir=config.default_download_dir(),
            configPath=config.config_path(),
        )

    # ---------- 局域网发现与配对 ----------
    @app.get("/api/pc/peers")
    def pc_peers():
        """本机能看到谁、已经记住了谁。"""
        if not _is_local():
            return jsonify(ok=False), 403
        paired = {p["id"]: p for p in pairing.list_peers()}
        nearby = []
        if discovery is not None:
            for item in discovery.peers():
                item["paired"] = item["id"] in paired
                nearby.append(item)
        return jsonify(
            ok=True,
            me=device.info(),
            nearby=nearby,
            paired=sorted(paired.values(), key=lambda x: x["lastSeen"], reverse=True),
            discovery=(discovery.snapshot() if discovery is not None
                       else {"enabled": False, "error": "", "port": 0, "peers": []}),
        )

    @app.post("/api/pc/discovery/refresh")
    def pc_discovery_refresh():
        if not _is_local():
            return jsonify(ok=False), 403
        if discovery is None:
            return jsonify(ok=False, error="发现服务没有启动"), 503
        discovery.query()
        return jsonify(ok=True, peers=discovery.peers())

    @app.delete("/api/pc/peers/<pid>")
    def pc_peer_remove(pid: str):
        if not _is_local():
            return jsonify(ok=False), 403
        pairing.revoke_tickets(pid)
        if not pairing.remove_peer(pid):
            return jsonify(ok=False, error="没有这台设备的记录"), 404
        return jsonify(ok=True)

    @app.post("/api/pc/peer/connect")
    def pc_peer_connect():
        """本机主动去连局域网里的另一台电脑。"""
        if not _is_local():
            return jsonify(ok=False), 403
        if discovery is None:
            return jsonify(ok=False, error="发现服务没有启动"), 503
        body = request.get_json(silent=True) or {}
        pid = str(body.get("id") or "").strip()
        target = next((p for p in discovery.peers() if p["id"] == pid), None)
        if target is None:
            return jsonify(ok=False, error="这台电脑已经不在线了，点「重新搜索」再看看"), 404

        base = f"http://{target['ip']}:{target['port']}"
        payload: dict = {"id": device.device_id(), "name": device.display_name()}
        known = pairing.get_peer(pid)
        if known and known.get("secret"):
            ts, sig = pairing.make_proof(known["secret"], device.device_id())
            payload["ts"] = ts
            payload["proof"] = sig
        try:
            reply = _peer_json(base, "/api/discovery/pair", payload)
        except PeerError as exc:
            return jsonify(ok=False, error=f"连不上 {target['ip']}（{exc}）"), 502

        if reply.get("status") == "paired" and reply.get("ticket"):
            url = f"{base}/p/{reply['ticket']}"
            _open_peer(url)
            return jsonify(ok=True, status="paired", url=url, target=target)

        rid = str(reply.get("rid") or "")
        if not rid:
            return jsonify(ok=False, error=str(reply.get("error") or "对端没有给出回应")), 502
        already = bool(known and known.get("secret"))
        with _outgoing_lock:
            _outgoing[rid] = {"base": base, "id": pid, "ip": target["ip"],
                              "name": target.get("name") or "", "ts": time.time()}
        who = target.get("name") or target["ip"]
        if already:
            msg = f"「{who}」那边已经解除过配对，请在它上面重新点「允许」"
        else:
            msg = f"已向「{who}」发出请求，请在那台电脑上点「允许」"
        return jsonify(ok=True, status="pending", rid=rid, target=target, message=msg)

    @app.get("/api/pc/peer/status/<rid>")
    def pc_peer_status(rid: str):
        """等对方主人点「允许」。界面每秒问一次。"""
        if not _is_local():
            return jsonify(ok=False), 403
        with _outgoing_lock:
            info = dict(_outgoing.get(rid) or {})
        if not info:
            return jsonify(ok=False, status="expired", error="这次请求已经结束了"), 404
        if time.time() - info["ts"] > pairing.PENDING_TTL + 30:
            with _outgoing_lock:
                _outgoing.pop(rid, None)
            return jsonify(ok=False, status="expired", error="等太久没回应，请重新连一次"), 410
        try:
            reply = _peer_json(info["base"], f"/api/pair/status/{rid}")
        except PeerError as exc:
            return jsonify(ok=False, status="error", error=f"和对方失去联系（{exc}）"), 502

        status = reply.get("status")
        if status == "allowed" and reply.get("ticket"):
            url = f"{info['base']}/p/{reply['ticket']}"
            secret = str(reply.get("secret") or "")
            name = str(reply.get("name") or info.get("name") or "")
            if secret:
                pairing.add_peer(info["id"], name, info["ip"], secret)
            with _outgoing_lock:
                _outgoing.pop(rid, None)
            _open_peer(url)
            return jsonify(ok=True, status="allowed", url=url)
        if status == "denied":
            with _outgoing_lock:
                _outgoing.pop(rid, None)
            return jsonify(ok=True, status="denied", error="对方拒绝了这次连接")
        if status != "pending":
            with _outgoing_lock:
                _outgoing.pop(rid, None)
            return jsonify(ok=False, status="expired",
                           error=str(reply.get("error") or "这次请求已经结束了"))
        return jsonify(ok=True, status="pending")

    @app.get("/api/pair/pending")
    def pair_pending_list():
        if not _is_local():
            return jsonify(ok=False), 403
        return jsonify(ok=True, items=pairing.pending_list(), me=device.info())

    @app.post("/api/pair/pending/<rid>/allow")
    def pair_allow(rid: str):
        if not _is_local():
            return jsonify(ok=False), 403
        item = pairing.pending_settle(rid, True)
        if item is None:
            return jsonify(ok=False, error="这条请求已经失效了"), 404
        return jsonify(ok=True, name=item["name"], id=item["id"])

    @app.post("/api/pair/pending/<rid>/deny")
    def pair_deny(rid: str):
        if not _is_local():
            return jsonify(ok=False), 403
        item = pairing.pending_settle(rid, False)
        if item is None:
            return jsonify(ok=False, error="这条请求已经失效了"), 404
        return jsonify(ok=True)

    # ---- 下面两个是「别的电脑来敲门」的接口，允许匿名访问 ----
    @app.post("/api/discovery/pair")
    def discovery_pair():
        """对端请求连接。认得出（proof 有效）就直接发票据，认不出就挂起等主人点。"""
        if not _pair_rate_ok(request.remote_addr or ""):
            return jsonify(ok=False, error="请求太频繁，稍等一会儿再试"), 429
        body = request.get_json(silent=True) or {}
        pid = str(body.get("id") or "").strip()
        if not re.fullmatch(r"[0-9a-f]{16,64}", pid):
            return jsonify(ok=False, error="设备标识不合法"), 400
        name = re.sub(r"\s+", " ", str(body.get("name") or "")).strip()[:24] or "另一台电脑"
        ip = request.remote_addr or ""
        ts, proof = body.get("ts"), body.get("proof")
        if ts is not None and isinstance(proof, str) and pairing.verify_proof(pid, ts, proof):
            pairing.touch(pid, ip)
            return jsonify(ok=True, status="paired",
                           ticket=pairing.issue_ticket(pid),
                           id=device.device_id(), name=device.display_name())
        item = pairing.pending_create(pid, name, ip)
        return jsonify(ok=True, status="pending", rid=item["rid"],
                       id=device.device_id(), name=device.display_name())

    @app.get("/api/pair/status/<rid>")
    def discovery_pair_status(rid: str):
        item = pairing.pending_get(rid)
        if item is None:
            return jsonify(ok=False, status="expired", error="请求已过期"), 404
        if item["state"] == "pending":
            return jsonify(ok=True, status="pending")
        if item["state"] == "denied":
            return jsonify(ok=True, status="denied", error="对方拒绝了这次连接")
        # 只把结果发给当初发起请求的那台机器，rid 被猜中也拿不走
        if item["ip"] and request.remote_addr != item["ip"]:
            return jsonify(ok=False, status="expired", error="请求来源不匹配"), 403
        item = pairing.pending_consume(rid) or item
        return jsonify(ok=True, status="allowed", ticket=item["ticket"],
                       secret=item["secret"], name=device.display_name())

    # ---------- 接收目录配置 ----------
    @app.get("/api/config")
    def get_config():
        if not _is_local():
            return jsonify(ok=False), 403
        return jsonify(
            ok=True,
            downloadDir=store.download_dir,
            source=store.dir_source,
            error=store.dir_error,
            defaultDir=config.default_download_dir(),
            configPath=config.config_path(),
        )

    @app.post("/api/config")
    def set_config():
        """改接收目录：校验 → 落盘 → 立即生效（不用重启）。

        body: {"downloadDir": "D:\\\\接收"}；传 null / 空串表示恢复默认。
        """
        if not _is_local():
            return jsonify(ok=False, error="仅本机可改"), 403
        data = request.get_json(silent=True) or {}
        raw = data.get("downloadDir")
        if raw in (None, ""):
            ok, error, path = config.reset_download_dir()
            source = "default"
        else:
            ok, error, path = config.save_download_dir(str(raw))
            source = "config"
        if not ok:
            # 不生效：内存里还是旧目录，前端据此提示
            return jsonify(ok=False, error=error, downloadDir=store.download_dir), 400
        store.set_download_dir(path, source)
        print(f"[config] 接收目录已切换：{path}（{source}）")
        return jsonify(ok=True, downloadDir=path, source=source)

    @app.post("/api/pc/pickdir")
    def pc_pickdir():
        """弹 Windows 原生文件夹选择框，返回用户选的路径（不直接生效）。"""
        if not _is_local():
            return jsonify(ok=False), 403
        from . import picker
        with _pick_lock:
            try:
                path = picker.pick_folder(initial=store.download_dir)
            except Exception as exc:  # noqa: BLE001
                return jsonify(ok=False, error=str(exc)), 500
        return jsonify(ok=True, path=path, cancelled=not path)

    @app.get("/api/qr.png")
    def qr_png():
        ip = request.args.get("ip") or host_ip
        target = f"http://{ip}:{port}/j/{token}"
        img = qrcode.make(target, box_size=8, border=2)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        buf.seek(0)
        resp = Response(buf.read(), mimetype="image/png")
        resp.headers["Cache-Control"] = "no-store"
        return resp

    # ---------- 状态 ----------
    @app.get("/api/state")
    def state():
        snap = store.snapshot()
        snap["ok"] = True
        snap["uploads"] = uploads.snapshot()
        snap["shot"] = dict(_shot_info)
        # 局域网里看见的电脑、等主人确认的配对请求、已经记住的设备
        snap["nearby"] = discovery.peers() if discovery is not None else []
        snap["pending"] = pairing.pending_list()
        snap["paired"] = pairing.list_peers()
        # 手机/对端页面靠这两个字段说清「连到了哪台电脑」
        snap["hostName"] = device.display_name()
        snap["isPeer"] = bool(request.cookies.get("wl_peer"))
        return jsonify(snap)

    # ---------- 上传（手机 → 电脑）----------
    @app.put("/api/upload")
    def upload():
        raw_name = request.args.get("name", "unnamed")
        local = _is_local()
        origin = "pc" if local else "phone"
        try:
            size = int(request.headers.get("Content-Length") or 0)
        except ValueError:
            size = 0
        final_name, dest = store.prepare_upload(raw_name, staging=local)
        uid = uploads.start(final_name, size)
        written = 0
        try:
            with open(dest, "wb") as fh:
                while True:
                    chunk = request.stream.read(CHUNK)
                    if not chunk:
                        break
                    fh.write(chunk)
                    written += len(chunk)
                    uploads.update(uid, written)
            if written == 0 and size > 0:
                raise OSError("空文件流")
        except Exception:
            uploads.finish(uid)
            try:
                os.remove(dest)
            except OSError:
                pass
            raise
        uploads.finish(uid)
        item = store.add_file(dest, origin, name=final_name, temporary=local)
        if item is None:
            return jsonify(ok=False, error="写入失败"), 500
        if not local:
            store.touch_phone()
        return jsonify(ok=True, id=item.id, name=item.name, savedDir=store.download_dir)

    # ---------- 下载（电脑 → 手机）----------
    @app.get("/api/download/<fid>")
    def download(fid: str):
        item = store.get(fid)
        if item is None:
            return jsonify(ok=False, error="文件记录不存在"), 404
        if not os.path.isfile(item.path):
            return jsonify(ok=False, error="源文件已被移动或删除"), 404
        if not request.headers.get("Range"):
            store.touch_download(fid)
        resp = send_file(item.path, as_attachment=True,
                         download_name=item.name, conditional=True)
        resp.headers["Content-Disposition"] = _disposition("attachment", item.name)
        resp.headers["X-Content-Type-Options"] = "nosniff"
        return resp

    @app.get("/api/img/<fid>")
    def image_inline(fid: str):
        """内联渲染原图：手机浏览器里直接显示，长按即可「保存图片」进相册。"""
        item = store.get(fid)
        if item is None or not os.path.isfile(item.path):
            return jsonify(ok=False, error="文件不存在"), 404
        mime = _sniff_image_mime(item.path)
        if mime is None:
            return jsonify(ok=False, error="这个文件不是图片"), 415
        resp = send_file(item.path, mimetype=mime, conditional=True)
        resp.headers["Content-Disposition"] = _disposition("inline", item.name)
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Cache-Control"] = "private, max-age=600"
        return resp

    @app.get("/api/thumb/<fid>")
    def thumb(fid: str):
        item = store.get(fid)
        if item is None or not os.path.isfile(item.path):
            return jsonify(ok=False, error="文件不存在"), 404
        try:
            width = int(request.args.get("w", 200))
        except ValueError:
            width = 200
        width = max(64, min(480, width))
        try:
            stamp = os.path.getmtime(item.path)
        except OSError:
            stamp = 0
        key = (fid, width, round(stamp, 3))
        with _thumb_lock:
            data = _thumb_cache.get(key)
        if data is None:
            try:
                with Image.open(item.path) as im:
                    im = im.convert("RGB")
                    im.thumbnail((width, width * 4))
                    buf = io.BytesIO()
                    im.save(buf, "JPEG", quality=82, optimize=True)
                    data = buf.getvalue()
            except Exception:  # noqa: BLE001
                return jsonify(ok=False, error="无法生成缩略图"), 415
            with _thumb_lock:
                if len(_thumb_cache) > 400:
                    _thumb_cache.clear()
                _thumb_cache[key] = data
        resp = Response(data, mimetype="image/jpeg")
        resp.headers["Cache-Control"] = "private, max-age=600"
        return resp

    # ---------- 文本互传 ----------
    @app.post("/api/text")
    def push_text():
        data = request.get_json(silent=True) or {}
        content = (data.get("content") or "").strip()
        origin = data.get("origin") or "phone"
        if not content:
            return jsonify(ok=False, error="内容为空"), 400
        if len(content) > 100_000:
            return jsonify(ok=False, error="文本过长"), 413
        item = store.add_text(content, origin)
        return jsonify(ok=True, id=item.id)

    @app.delete("/api/text")
    def drop_texts():
        store.clear_texts()
        return jsonify(ok=True)

    # ---------- 文件管理 ----------
    @app.delete("/api/file/<fid>")
    def delete_file(fid: str):
        return jsonify(ok=store.remove(fid))

    @app.post("/api/clear")
    def clear():
        data = request.get_json(silent=True) or {}
        n = store.clear(data.get("origin"))
        return jsonify(ok=True, removed=n)

    # ---------- 桌面端专属 ----------
    @app.post("/api/pc/pick")
    def pc_pick():
        """弹出 Windows 原生文件框，把选中的文件零拷贝挂给手机。"""
        if not _is_local():
            return jsonify(ok=False), 403
        from . import picker
        with _pick_lock:
            try:
                paths = picker.pick_files()
            except Exception as exc:  # noqa: BLE001
                return jsonify(ok=False, error=str(exc)), 500
        added = []
        for p in paths:
            item = store.add_file(p, "pc")
            if item:
                added.append(item.to_dict())
        return jsonify(ok=True, added=added, cancelled=not paths)

    @app.post("/api/pc/add")
    def pc_add():
        """登记本机已有文件（零拷贝：不复制、不上传，直接把路径挂给手机下载）。"""
        if not _is_local():
            return jsonify(ok=False), 403
        data = request.get_json(silent=True) or {}
        paths = data.get("paths") or []
        added = []
        for p in paths:
            item = store.add_file(p, "pc")
            if item:
                added.append(item.to_dict())
        return jsonify(ok=True, added=added, skipped=len(paths) - len(added))

    @app.post("/api/pc/reveal/<fid>")
    def pc_reveal(fid: str):
        if not _is_local():
            return jsonify(ok=False), 403
        item = store.get(fid)
        if item and os.path.exists(item.path):
            try:
                subprocess.Popen(["explorer", "/select,", os.path.normpath(item.path)])
                return jsonify(ok=True)
            except OSError as exc:
                return jsonify(ok=False, error=str(exc)), 500
        return jsonify(ok=False, error="文件不存在"), 404

    @app.post("/api/pc/copyfile/<fid>")
    def pc_copyfile(fid: str):
        """把文件以「文件」形式放进 Windows 剪贴板（等价于资源管理器里 Ctrl+C）。"""
        if not _is_local():
            return jsonify(ok=False), 403
        item = store.get(fid)
        if item is None:
            return jsonify(ok=False, error="文件记录不存在"), 404
        if not os.path.isfile(item.path):
            return jsonify(ok=False, error="源文件已被移动或删除"), 404
        from . import clipboard
        try:
            clipboard.copy_files([item.path])
        except clipboard.ClipboardError as exc:
            # 剪贴板被别的程序占着、没权限读、内存不足都从这里出
            return jsonify(ok=False, error=str(exc)), 503
        except OSError as exc:
            return jsonify(ok=False, error=f"复制失败：{exc}"), 500
        return jsonify(ok=True, name=item.name, path=item.path)

    @app.post("/api/pc/opendir")
    def pc_opendir():
        if not _is_local():
            return jsonify(ok=False), 403
        target = store.download_dir
        # 目录可能被用户事后删掉/移走，打开前再兜一次
        ok, error, target = config.validate_dir(target)
        if not ok:
            return jsonify(ok=False, error=f"接收目录不可用（{error}）"), 400
        try:
            os.startfile(target)  # type: ignore[attr-defined]
            return jsonify(ok=True, dir=target)
        except OSError as exc:
            return jsonify(ok=False, error=str(exc)), 500

    @app.post("/api/pc/clipboard")
    def pc_clipboard():
        """把文字写进 Windows 剪贴板（手机发来的文字自动落盘到剪贴板）。"""
        if not _is_local():
            return jsonify(ok=False), 403
        data = request.get_json(silent=True) or {}
        content = data.get("content", "")
        if not content:
            return jsonify(ok=False, error="内容为空"), 400
        try:
            subprocess.run(["clip"], input=content.encode("utf-16le"), check=True,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            return jsonify(ok=True)
        except Exception as exc:  # noqa: BLE001
            return jsonify(ok=False, error=str(exc)), 500

    @app.get("/api/pc/path/<fid>")
    def pc_path(fid: str):
        if not _is_local():
            return jsonify(ok=False), 403
        item = store.get(fid)
        if item is None:
            return jsonify(ok=False), 404
        return jsonify(ok=True, path=item.path)

    @app.get("/api/ip")
    def ip_list():
        if not _is_local():
            return jsonify(ok=False), 403
        cards = netutil.nics()
        return jsonify(ok=True, ips=[c.ip for c in cards],
                       nics=[c.to_dict() for c in cards])

    # ---------- 截图发送 ----------
    @app.post("/api/pc/screenshot")
    def pc_screenshot():
        """抓整屏 → 立刻开整屏覆盖层让用户在真实分辨率上框选。

        抓完主窗口保持最小化，直到 /send、DELETE 或 /close 才恢复。
        """
        if not _is_local():
            return jsonify(ok=False), 403
        from . import desktop, screen
        hint = f"({port})"
        with _shot_lock:
            screen.close_shot_window(hint)      # 清掉可能残留的上一个覆盖窗
            try:
                meta = screen.capture(hide_hint=hint)
            except Exception as exc:  # noqa: BLE001
                return jsonify(ok=False, error=f"抓屏失败：{exc}"), 500
            url = (f"http://127.0.0.1:{port}/shot"
                   f"?sid={meta['id']}&port={port}")
            if not desktop.open_shot_window(url, port):
                # 覆盖层起不来，别把主窗口扔在最小化里出不来
                screen.discard(meta["id"])
                screen.restore_main(hint)
                return jsonify(ok=False, error="打不开截图窗口：没找到 Edge"), 500
        return jsonify(ok=True, **meta)

    @app.get("/api/pc/screenshot/<sid>")
    def pc_screenshot_view(sid: str):
        from . import screen
        path = screen.temp_path(sid)
        if not path or not os.path.exists(path):
            return jsonify(ok=False, error="截图已过期"), 404
        resp = send_file(path, mimetype="image/png")
        resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.post("/api/pc/screenshot/<sid>/send")
    def pc_screenshot_send(sid: str):
        if not _is_local():
            return jsonify(ok=False), 403
        from . import screen
        data = request.get_json(silent=True) or {}
        box = None
        if not data.get("full"):
            try:
                box = (int(data["x"]), int(data["y"]), int(data["w"]), int(data["h"]))
            except (KeyError, TypeError, ValueError):
                return jsonify(ok=False, error="选区参数不完整"), 400
            if box[2] < 4 or box[3] < 4:
                return jsonify(ok=False, error="选区太小了"), 400
        stem = time.strftime("截图 %Y-%m-%d %H-%M-%S")
        # 先确认目录还能用：commit() 会消耗掉暂存截图，不能等它失败了再后悔
        ok, error, out_dir = config.validate_dir(store.download_dir)
        if not ok:
            return jsonify(ok=False, error=f"接收目录不可用（{error}），截图未发送"), 400
        try:
            got = screen.commit(sid, box, out_dir, stem)
        except Exception as exc:  # noqa: BLE001
            return jsonify(ok=False, error=f"保存失败：{exc}"), 500
        if got is None:
            return jsonify(ok=False, error="截图已过期，请重新截取"), 404
        name, dest = got
        item = store.add_file(dest, "pc", name=name)
        if item is None:
            return jsonify(ok=False, error="写入失败"), 500

        # 发送的同时把这张图放进剪贴板：用户往往截完就想贴进聊天窗口/文档，
        # 少一步「再去文件夹里找」。顺序是先落盘、再进剪贴板、最后才回响应，
        # 所以前端收到 ok 时两件事都已经做完。
        # 复制失败不回滚也不报整体失败：图已经存好、手机也能取，只是少了个便利，
        # 用 clip.ok=false 带原因回去，由界面提示，别让用户以为整次截图白截了。
        clip_ok, clip_error = True, ""
        from . import clipboard
        try:
            clipboard.copy_image(dest)
        except clipboard.ClipboardError as exc:
            clip_ok, clip_error = False, str(exc)
        except OSError as exc:
            clip_ok, clip_error = False, f"复制图片失败：{exc}"

        _note_shot(ok=True, name=name, clip_ok=clip_ok, error=clip_error)
        _finish_shot(screen, f"({port})")
        return jsonify(ok=True, item=item.to_dict(), dir=out_dir,
                       clip={"ok": clip_ok, "error": clip_error})

    @app.delete("/api/pc/screenshot/<sid>")
    def pc_screenshot_drop(sid: str):
        if not _is_local():
            return jsonify(ok=False), 403
        from . import screen
        gone = screen.discard(sid)
        _finish_shot(screen, f"({port})")
        return jsonify(ok=gone)

    @app.post("/api/pc/screenshot/<sid>/close")
    def pc_screenshot_close(sid: str):
        """覆盖层被直接关掉（Alt+F4 / 点关闭）时的兜底。

        页面卸载时用 sendBeacon 打过来：把暂存图丢掉，再把主窗口放回来，
        否则用户会看到主窗口一直躺在任务栏里最小化着。
        """
        if not _is_local():
            return jsonify(ok=False), 403
        from . import screen
        gone = screen.discard(sid)
        screen.close_shot_window(f"({port})")
        screen.restore_main(f"({port})")
        return jsonify(ok=True, discarded=gone)

    return app


def _finish_shot(screen, hint: str) -> None:
    """收尾：关掉框选覆盖层，再把主窗口放回前台。

    必须等响应先发出去 —— 覆盖层页面正等着 /send 的结果，
    立刻关窗会让它的 fetch 断在半路。所以扔到定时器里晚 0.4s 做。
    """
    def run() -> None:
        time.sleep(0.4)
        try:
            screen.close_shot_window(hint)
            time.sleep(0.25)
            screen.restore_main(hint)
        except Exception:  # noqa: BLE001
            pass

    threading.Thread(target=run, daemon=True).start()

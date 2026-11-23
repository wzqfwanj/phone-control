#!/usr/bin/env python3
"""手机远程控制 - 单进程 Python 服务端（PROTOCOL.md v1）。

一个 asyncio 进程同时提供：

* HTTP:
    - GET /healthz                      -> {"ok": true}
    - GET /<其他路径>                    -> 静态站点（CONTROL_STATIC_DIR，默认 web/dist）
      规则："/" -> index.html；未知路径回退 index.html；index.html 不缓存。
      静态目录不存在时所有页面路径 404，但 WebSocket 通道照常工作。
* WebSocket:
    - /ws/device?key=<KEY>              手机设备通道（key 与 CONTROL_DEVICE_KEY 完全一致）
    - /ws/web                           浏览器通道

所有路径先剥掉 `/agent` 前缀再路由（线上 Caddy 用 `handle /agent/*` 转发设备连接）。

实现要点（与协议一一对应）：
* 设备鉴权在 WebSocket 握手（upgrade）之前完成：不匹配直接返回 HTTP 401。
* 同一时刻只保留一台设备；新设备连上时先给旧设备发 {"t":"bye","reason":"replaced"} 再关闭。
* 设备二进制帧 = 一个完整 JPEG：缓存最后一帧，转发给所有 view=on 的浏览器。
* 浏览器指令由服务端补 id（c1、c2...）后转发设备；设备的 ack/error 按 id 回到发起浏览器。
* 观看人数变化时给设备推 {"t":"viewers","n":N}；设备连上时也会收到当前人数。
* 心跳：客户端文本 {"t":"ping"} -> 回 {"t":"pong"}；服务端每 25s 发文本 ping；
  60s 收不到任何帧（含二进制）就断开该连接。不使用 WebSocket 控制帧 ping（协议要求文本帧）。

环境变量：
    CONTROL_HOST        监听地址，默认 127.0.0.1
    CONTROL_PORT        端口，默认 8090
    CONTROL_DEVICE_KEY  设备配对码；为空则拒绝所有设备连接
    CONTROL_STATIC_DIR  静态目录，默认 web/dist（相对当前工作目录）

依赖：仅 websockets（见 requirements.txt）。
"""

from __future__ import annotations

import asyncio
import contextlib
import email.utils
import http
import json
import logging
import mimetypes
import os
import secrets
import sys
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from websockets.asyncio.server import ServerConnection, serve
from websockets.datastructures import Headers
from websockets.exceptions import ConnectionClosed
from websockets.http11 import Request, Response

logger = logging.getLogger("phone_control.server")

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

DEVICE_PATH = "/ws/device"
WEB_PATH = "/ws/web"
AGENT_PREFIX = "/agent"

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8090
DEFAULT_STATIC_DIR = "web/dist"

PING_INTERVAL = 25.0  # 服务端 -> 客户端 文本 ping 间隔
IDLE_TIMEOUT = 60.0  # 超过该时长没收到任何帧就断开
CMD_TIMEOUT = 30.0  # 指令等待设备 ack/error 的最长时长
HEARTBEAT_TICK = 5.0  # 后台巡检周期
CLOSE_TIMEOUT = 3.0  # WebSocket close 握手超时
MAX_MESSAGE_SIZE = 8 * 1024 * 1024  # 单条消息上限（JPEG 帧远小于此）

# Vite 产物常见后缀；不依赖 Windows 注册表的 MIME 映射。
_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".htm": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".map": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".ico": "image/x-icon",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".ttf": "font/ttf",
    ".txt": "text/plain; charset=utf-8",
    ".wasm": "application/wasm",
    ".apk": "application/vnd.android.package-archive",
}


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Config:
    """服务端运行配置（默认值即 PROTOCOL.md 约定值）。"""

    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    device_key: str = ""
    static_dir: Path = Path(DEFAULT_STATIC_DIR)
    ping_interval: float = PING_INTERVAL
    idle_timeout: float = IDLE_TIMEOUT
    tick: float = HEARTBEAT_TICK

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "Config":
        env = os.environ if env is None else env
        raw_port = env.get("CONTROL_PORT", str(DEFAULT_PORT)).strip() or str(DEFAULT_PORT)
        try:
            port = int(raw_port)
        except ValueError as exc:
            raise ValueError(f"CONTROL_PORT 不是合法端口: {raw_port!r}") from exc
        if not 0 <= port <= 65535:
            raise ValueError(f"CONTROL_PORT 超出范围: {port}")
        return cls(
            host=env.get("CONTROL_HOST", DEFAULT_HOST) or DEFAULT_HOST,
            port=port,
            device_key=env.get("CONTROL_DEVICE_KEY", ""),
            static_dir=Path(env.get("CONTROL_STATIC_DIR", DEFAULT_STATIC_DIR) or DEFAULT_STATIC_DIR),
        )


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def normalize_path(raw_path: str) -> str:
    """把请求路径规范化为服务端内部路由：解码、剥掉 `/agent` 前缀、去掉 query。"""
    path = urllib.parse.urlsplit(raw_path).path or "/"
    path = urllib.parse.unquote(path)
    if path == AGENT_PREFIX:
        return "/"
    if path.startswith(AGENT_PREFIX + "/"):
        path = path[len(AGENT_PREFIX):]
    return path


def _request_query(raw_path: str) -> dict[str, list[str]]:
    return urllib.parse.parse_qs(urllib.parse.urlsplit(raw_path).query, keep_blank_values=True)


def _parse_json_message(raw: str) -> dict[str, Any] | None:
    """解析 JSON 文本帧；只接受 JSON object，其余返回 None。"""
    try:
        msg = json.loads(raw)
    except (ValueError, TypeError):
        return None
    return msg if isinstance(msg, dict) else None


def _content_type(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in _CONTENT_TYPES:
        return _CONTENT_TYPES[suffix]
    guessed, _ = mimetypes.guess_type(str(path))
    return guessed or "application/octet-stream"


def _reason_phrase(status: int) -> str:
    try:
        return http.HTTPStatus(status).phrase
    except ValueError:  # pragma: no cover - 不会对本模块用到的状态码触发
        return "OK"


def _http_response(status: int, body: bytes, content_type: str, *, cache_control: str | None = None,
                   extra_headers: Iterable[tuple[str, str]] = ()) -> Response:
    """构造一个普通的（非 101）HTTP 响应；发送后连接会被关闭。"""
    headers = Headers(
        [
            ("Date", email.utils.formatdate(usegmt=True)),
            ("Connection", "close"),
            ("Content-Type", content_type),
            ("Content-Length", str(len(body))),
            ("X-Content-Type-Options", "nosniff"),
        ]
    )
    if cache_control:
        headers["Cache-Control"] = cache_control
    for key, value in extra_headers:
        headers[key] = value
    return Response(status, _reason_phrase(status), headers, body)


def _text_response(status: int, text: str, *, extra_headers: Iterable[tuple[str, str]] = ()) -> Response:
    return _http_response(
        status,
        text.encode("utf-8"),
        "text/plain; charset=utf-8",
        cache_control="no-store",
        extra_headers=extra_headers,
    )


def _json_response(payload: Any, *, status: int = 200) -> Response:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return _http_response(status, body, "application/json; charset=utf-8", cache_control="no-store")


# ---------------------------------------------------------------------------
# 会话
# ---------------------------------------------------------------------------


@dataclass(eq=False)
class DeviceSession:
    """一台已连接的手机设备。"""

    ws: ServerConnection
    last_recv: float = field(default_factory=time.monotonic)
    info: dict[str, Any] | None = None  # 最近一次 hello 的 {name,w,h,accessibility}
    frames_on: bool | None = None


@dataclass(eq=False)
class BrowserSession:
    """一个已连接的浏览器。"""

    ws: ServerConnection
    last_recv: float = field(default_factory=time.monotonic)
    view: bool = False
    # 单槽队列：新帧覆盖尚未发出的旧帧，消费者慢时只丢帧不阻塞设备。
    frame_queue: asyncio.Queue[bytes] = field(default_factory=lambda: asyncio.Queue(maxsize=1))
    writer_task: asyncio.Task[None] | None = None

    def offer_frame(self, frame: bytes) -> None:
        if self.frame_queue.full():
            with contextlib.suppress(asyncio.QueueEmpty):
                self.frame_queue.get_nowait()
        with contextlib.suppress(asyncio.QueueFull):
            self.frame_queue.put_nowait(frame)


# ---------------------------------------------------------------------------
# 服务端
# ---------------------------------------------------------------------------


class ControlServer:
    """HTTP 静态站点 + 两条 WebSocket 通道的单进程中继服务。"""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.device: DeviceSession | None = None
        self.browsers: set[BrowserSession] = set()
        self.last_frame: bytes | None = None
        # 指令 id -> (发起的浏览器, 超时时刻)
        self.pending: dict[str, tuple[BrowserSession, float]] = {}
        self._next_cmd_id = 0
        self._server: Any = None
        self._ticker_task: asyncio.Task[None] | None = None

    # -- 生命周期 ---------------------------------------------------------

    async def start(self) -> None:
        self._server = await serve(
            self._handle_connection,
            self.config.host,
            self.config.port,
            process_request=self._process_request,
            ping_interval=None,  # 心跳用协议规定的文本帧，不用 WS 控制帧
            ping_timeout=None,
            close_timeout=CLOSE_TIMEOUT,
            compression=None,  # JPEG 帧无需压缩，省 CPU
            max_size=MAX_MESSAGE_SIZE,
            server_header="phone-control",
        )
        self._ticker_task = asyncio.create_task(self._heartbeat_loop(), name="heartbeat")
        logger.info(
            "listening on http://%s:%d/ (device path %s, web path %s, static %s)",
            self.config.host,
            self.bound_port,
            DEVICE_PATH,
            WEB_PATH,
            self.config.static_dir,
        )

    async def stop(self) -> None:
        if self._ticker_task is not None:
            self._ticker_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._ticker_task
            self._ticker_task = None
        if self._server is not None:
            self._server.close(close_connections=True)
            await self._server.wait_closed()
            self._server = None
            logger.info("server stopped")

    @property
    def bound_port(self) -> int:
        """实际监听端口（测试可用 port=0 让系统分配）。"""
        if self._server is None:
            return self.config.port
        sockets = self._server.sockets
        return sockets[0].getsockname()[1] if sockets else self.config.port

    # -- HTTP / 握手 ------------------------------------------------------

    async def _process_request(self, connection: ServerConnection, request: Request) -> Response | None:
        """WebSocket 握手前拦截：设备鉴权、HTTP 静态站点。返回 None 表示继续 WS 握手。"""
        path = normalize_path(request.path)

        if path == DEVICE_PATH:
            supplied = (_request_query(request.path).get("key") or [""])[0]
            if not self._device_key_matches(supplied):
                logger.info("device connection rejected: bad key")
                return _text_response(401, "unauthorized: invalid device key\n")
            return None

        if path == WEB_PATH:
            return None

        if path.startswith("/ws/"):
            return _text_response(404, "not found: unknown websocket endpoint\n")

        return self._static_response(request, path)

    def _device_key_matches(self, supplied: str) -> bool:
        expected = self.config.device_key
        if not expected:
            return False  # 未配置配对码时拒绝所有设备
        return secrets.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8"))

    def _static_response(self, request: Request, path: str) -> Response:
        if request.method not in ("GET", "HEAD"):
            return _text_response(405, "method not allowed\n", extra_headers=[("Allow", "GET, HEAD")])

        if path == "/healthz":
            return _json_response({"ok": True})

        root = Path(self.config.static_dir)
        target = self._resolve_static_file(root, path)
        if target is None:
            return _text_response(404, "not found\n")

        try:
            body = b"" if request.method == "HEAD" else target.read_bytes()
            size = target.stat().st_size
        except OSError as exc:
            logger.warning("failed to read static file %s: %s", target, exc)
            return _text_response(500, "internal server error\n")

        is_index = target.name == "index.html"
        return _http_response(
            200,
            body,
            _content_type(target),
            cache_control="no-store" if is_index else "public, max-age=3600",
            extra_headers=[("Content-Length", str(size))] if request.method == "HEAD" else (),
        )

    def _resolve_static_file(self, root: Path, path: str) -> Path | None:
        """安全解析静态文件；未知路径回退 index.html；目录缺失返回 None。"""
        if not root.is_dir():
            return None
        root_resolved = root.resolve()
        rel = path.lstrip("/")
        if not rel or path.endswith("/"):
            rel = rel + "index.html" if rel else "index.html"
        candidate = root_resolved / rel
        try:
            resolved = candidate.resolve()
            is_file = resolved.is_file()
        except (OSError, ValueError):
            resolved = None
            is_file = False
        if (
            resolved is not None
            and is_file
            and _is_within(resolved, root_resolved)
        ):
            return resolved

        index = root_resolved / "index.html"
        if index.is_file():
            return index
        return None

    # -- WebSocket 总入口 --------------------------------------------------

    async def _handle_connection(self, ws: ServerConnection) -> None:
        path = normalize_path(ws.request.path)
        if path == DEVICE_PATH:
            await self._handle_device(ws)
        elif path == WEB_PATH:
            await self._handle_browser(ws)
        else:  # pragma: no cover - 握手阶段已拦截
            await ws.close(1008, "unknown route")

    # -- 设备通道 ---------------------------------------------------------

    async def _handle_device(self, ws: ServerConnection) -> None:
        session = DeviceSession(ws)
        old = self.device
        self.device = session
        logger.info("device connected")

        if old is not None:
            logger.info("replacing previous device")
            await self._fail_pending("device replaced")
            await self._send_json(old.ws, {"t": "bye", "reason": "replaced"})
            await self._close_ws(old.ws, "replaced")

        await self._send_json(ws, {"t": "viewers", "n": self._viewer_count()})
        # 不在这里广播 online=true：设备连上后会立即发 hello，届时带完整信息广播一次，
        # 避免给浏览器发 online=true 但 name/w/h 为 null 的中间状态。

        try:
            async for raw in ws:
                session.last_recv = time.monotonic()
                if isinstance(raw, bytes):
                    await self._on_device_frame(raw)
                else:
                    await self._on_device_text(session, raw)
        except ConnectionClosed:
            pass
        finally:
            if self.device is session:
                self.device = None
                self.last_frame = None
                await self._fail_pending("device offline")
                await self._broadcast_status(online=False)
                logger.info("device disconnected")

    async def _on_device_frame(self, frame: bytes) -> None:
        if not frame:
            return
        self.last_frame = frame
        for browser in list(self.browsers):
            if browser.view:
                browser.offer_frame(frame)

    async def _on_device_text(self, session: DeviceSession, raw: str) -> None:
        msg = _parse_json_message(raw)
        if msg is None:
            logger.debug("ignoring non-JSON text frame from device")
            return

        kind = msg.get("t")
        if kind == "hello":
            session.info = {
                "name": msg.get("name"),
                "w": msg.get("w"),
                "h": msg.get("h"),
                "accessibility": msg.get("accessibility"),
            }
            logger.info("device hello: %s", session.info)
            await self._broadcast_status(online=True)
        elif kind == "frames":
            session.frames_on = bool(msg.get("on"))
        elif kind in ("ack", "error"):
            await self._route_device_reply(session, msg, kind)
        elif kind == "pong":
            pass  # last_recv 已更新
        elif kind == "ping":
            await self._send_json(session.ws, {"t": "pong"})
        else:
            logger.debug("ignoring device message type %r", kind)

    async def _route_device_reply(self, session: DeviceSession, msg: dict[str, Any], kind: str) -> None:
        cmd_id = msg.get("id")
        if not isinstance(cmd_id, str):
            return
        entry = self.pending.pop(cmd_id, None)
        if entry is None:
            logger.debug("device reply for unknown command id %r", cmd_id)
            return
        browser, _deadline = entry
        if browser not in self.browsers:
            return
        reply: dict[str, Any] = {"t": kind, "id": cmd_id}
        if kind == "error":
            message = msg.get("message")
            reply["message"] = message if isinstance(message, str) and message else "command failed"
        await self._send_json(browser.ws, reply)

    # -- 浏览器通道 -------------------------------------------------------

    async def _handle_browser(self, ws: ServerConnection) -> None:
        browser = BrowserSession(ws)
        self.browsers.add(browser)
        browser.writer_task = asyncio.create_task(self._frame_writer(browser), name="frame-writer")
        logger.info("browser connected (total %d)", len(self.browsers))

        await self._send_json(ws, self._status_message(online=self.device is not None))

        try:
            async for raw in ws:
                browser.last_recv = time.monotonic()
                if isinstance(raw, bytes):
                    continue  # 浏览器不发二进制帧
                await self._on_browser_text(browser, raw)
        except ConnectionClosed:
            pass
        finally:
            self.browsers.discard(browser)
            for cmd_id in [cid for cid, (owner, _dl) in self.pending.items() if owner is browser]:
                self.pending.pop(cmd_id, None)
            if browser.writer_task is not None:
                browser.writer_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await browser.writer_task
            was_viewing = browser.view
            browser.view = False
            if was_viewing:
                await self._notify_viewers()
            logger.info("browser disconnected (total %d)", len(self.browsers))

    async def _on_browser_text(self, browser: BrowserSession, raw: str) -> None:
        msg = _parse_json_message(raw)
        if msg is None:
            logger.debug("ignoring non-JSON text frame from browser")
            return

        kind = msg.get("t")
        if kind == "view":
            await self._on_view(browser, bool(msg.get("on")))
        elif kind == "cmd":
            await self._on_browser_command(browser, msg)
        elif kind == "ping":
            await self._send_json(browser.ws, {"t": "pong"})
        elif kind == "pong":
            pass
        else:
            logger.debug("ignoring browser message type %r", kind)

    async def _on_view(self, browser: BrowserSession, on: bool) -> None:
        was_viewing = browser.view
        browser.view = on
        if on and not was_viewing and self.last_frame is not None:
            browser.offer_frame(self.last_frame)  # 新观看者秒显最后一帧
        if on != was_viewing:
            await self._notify_viewers()

    async def _on_browser_command(self, browser: BrowserSession, msg: dict[str, Any]) -> None:
        action = msg.get("action")
        if not isinstance(action, str) or not action:
            await self._send_json(
                browser.ws,
                {"t": "error", "id": self._allocate_cmd_id(), "message": "missing action"},
            )
            return

        cmd_id = self._allocate_cmd_id()
        device = self.device
        if device is None:
            await self._send_json(
                browser.ws, {"t": "error", "id": cmd_id, "message": "device offline"}
            )
            return

        forward: dict[str, Any] = {"t": "cmd", "id": cmd_id}
        for key, value in msg.items():
            if key not in ("t", "id"):
                forward[key] = value
        if await self._send_json(device.ws, forward):
            self.pending[cmd_id] = (browser, time.monotonic() + CMD_TIMEOUT)
        else:
            await self._send_json(
                browser.ws, {"t": "error", "id": cmd_id, "message": "device offline"}
            )

    def _allocate_cmd_id(self) -> str:
        self._next_cmd_id += 1
        return f"c{self._next_cmd_id}"

    async def _frame_writer(self, browser: BrowserSession) -> None:
        """把最新帧按顺序写给浏览器；队列单槽，慢客户端自动丢旧帧。"""
        try:
            while True:
                frame = await browser.frame_queue.get()
                await browser.ws.send(frame)
        except (ConnectionClosed, asyncio.CancelledError):
            pass

    # -- 状态 / viewers / 指令超时 ----------------------------------------

    def _viewer_count(self) -> int:
        return sum(1 for browser in self.browsers if browser.view)

    async def _notify_viewers(self) -> None:
        device = self.device
        if device is not None:
            await self._send_json(device.ws, {"t": "viewers", "n": self._viewer_count()})

    def _status_message(self, *, online: bool) -> dict[str, Any]:
        # 只有拿到 hello（设备名/分辨率/无障碍状态）后才对外报 online=true，
        # 这样线上状态消息里的字段永远不会是 null（协议 5.2）。
        info = self.device.info if (online and self.device is not None) else None
        if info is None:
            return {"t": "status", "online": False, "name": None, "w": None, "h": None,
                    "accessibility": None}
        return {
            "t": "status",
            "online": True,
            "name": info.get("name"),
            "w": info.get("w"),
            "h": info.get("h"),
            "accessibility": info.get("accessibility"),
        }

    async def _broadcast_status(self, *, online: bool) -> None:
        if not self.browsers:
            return
        message = self._status_message(online=online)
        await asyncio.gather(
            *(self._send_json(browser.ws, message) for browser in list(self.browsers)),
            return_exceptions=True,
        )

    async def _fail_pending(self, reason: str) -> None:
        pending, self.pending = self.pending, {}
        await asyncio.gather(
            *(
                self._send_json(browser.ws, {"t": "error", "id": cmd_id, "message": reason})
                for cmd_id, (browser, _deadline) in pending.items()
                if browser in self.browsers
            ),
            return_exceptions=True,
        )

    async def _expire_pending(self, now: float) -> None:
        expired = [
            (cmd_id, browser)
            for cmd_id, (browser, deadline) in self.pending.items()
            if deadline <= now
        ]
        for cmd_id, browser in expired:
            self.pending.pop(cmd_id, None)
            if browser in self.browsers:
                await self._send_json(
                    browser.ws, {"t": "error", "id": cmd_id, "message": "command timeout"}
                )

    # -- 心跳 / 空闲断开 --------------------------------------------------

    async def _heartbeat_loop(self) -> None:
        ping_interval = self.config.ping_interval
        idle_timeout = self.config.idle_timeout
        next_ping = time.monotonic() + ping_interval
        while True:
            await asyncio.sleep(self.config.tick)
            now = time.monotonic()

            for session in self._all_peers():
                if now - session.last_recv > idle_timeout:
                    logger.info("closing idle websocket (%s)", session.ws.request.path if session.ws.request else "?")
                    await self._close_ws(session.ws, "idle timeout")

            if now >= next_ping:
                next_ping = now + ping_interval
                await asyncio.gather(
                    *(self._send_json(session.ws, {"t": "ping"}) for session in self._all_peers()),
                    return_exceptions=True,
                )

            await self._expire_pending(now)

    def _all_peers(self) -> list[DeviceSession | BrowserSession]:
        peers: list[DeviceSession | BrowserSession] = list(self.browsers)
        if self.device is not None:
            peers.append(self.device)
        return peers

    # -- 发送辅助 ---------------------------------------------------------

    async def _send_json(self, ws: ServerConnection, message: dict[str, Any]) -> bool:
        try:
            await ws.send(json.dumps(message, ensure_ascii=False, separators=(",", ":")))
            return True
        except ConnectionClosed:
            return False

    async def _close_ws(self, ws: ServerConnection, reason: str) -> None:
        with contextlib.suppress(ConnectionClosed, OSError, asyncio.CancelledError):
            await ws.close(1000, reason)


def _is_within(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


async def run(config: Config) -> None:
    server = ControlServer(config)
    await server.start()
    static_dir = Path(config.static_dir)
    if static_dir.is_dir():
        logger.info("static dir: %s", static_dir.resolve())
    else:
        logger.warning("static dir %r not found; HTTP pages will return 404 until web/dist exists", str(static_dir))
    try:
        await asyncio.Event().wait()  # 永远等待；Ctrl+C / 进程退出走 finally
    finally:
        await server.stop()


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        config = Config.from_env()
    except ValueError as exc:
        logger.error("invalid configuration: %s", exc)
        return 2

    logger.info("phone-control server starting (python %s)", sys.version.split()[0])
    try:
        asyncio.run(run(config))
    except KeyboardInterrupt:
        logger.info("interrupted; exiting")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

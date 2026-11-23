#!/usr/bin/env python3
"""前端自测用假服务端（**不参与生产**，生产服务端在 server/server.py）。

同一端口同时提供：
- 静态文件：托管 ``web/dist``，行为贴近真实服务端（含 ``/control`` 前缀容错、未知路径回退 index.html）
- WebSocket ``ws/web``：模拟浏览器通道，下发 status + JPEG 帧，接收 view/cmd/ping 并记入 JSONL 日志

用法::

    cd web && npm run build
    python web/dev/mock_server.py --port 18093 --log C:\\temp\\mock.jsonl
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import io
import json
import mimetypes
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed
from websockets.http11 import Headers, Response

BASE_DIR = Path(__file__).resolve().parents[1]
DIST_DIR = BASE_DIR / "dist"

ONLINE_STATUS = {
    "t": "status",
    "online": True,
    "name": "Mock Pixel 7",
    "w": 1080,
    "h": 2400,
    "accessibility": True,
}
OFFLINE_STATUS = {
    "t": "status",
    "online": False,
    "name": None,
    "w": None,
    "h": None,
    "accessibility": None,
}

# 1x1 的合法 JPEG，作为没有 Pillow 时的兜底帧
FALLBACK_JPEG = base64.b64decode(
    "/9j/4AAQSkZJRgABAQEAYABgAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0a"
    "HBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/wAALCAABAAEBAREA/8QAFAABAAAAAAAA"
    "AAAAAAAAAAAACf/EABQQAQAAAAAAAAAAAAAAAAAAAAD/2gAIAQEAAD8AVN//2Q=="
)


def make_frame() -> bytes:
    """生成一张 270x600 的灰色 JPEG 假画面。"""
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return FALLBACK_JPEG
    image = Image.new("RGB", (270, 600), (58, 63, 71))
    draw = ImageDraw.Draw(image)
    draw.rectangle([24, 40, 246, 560], outline=(179, 185, 194), width=2)
    draw.text((70, 286), "MOCK SCREEN", fill=(242, 243, 245))
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality=60)
    return buffer.getvalue()


class MockServer:
    def __init__(self, port: int, log_path: Path | None, device_offline: bool = False) -> None:
        self.port = port
        self.log_path = log_path
        self.device_offline = device_offline
        self.frame = make_frame()
        self._lock = asyncio.Lock()

    # ---------- 日志 ----------

    async def log(self, record: dict) -> None:
        if self.log_path is None:
            return
        record = {"ts": round(time.time(), 3), **record}
        line = json.dumps(record, ensure_ascii=False)
        async with self._lock:
            with self.log_path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")

    # ---------- HTTP 静态 ----------

    def serve_static(self, raw_path: str) -> Response:
        rel = urlsplit(raw_path).path or "/"
        if rel == "/control":
            rel = "/"
        elif rel.startswith("/control/"):
            rel = rel[len("/control"):]
        if rel in ("", "/"):
            rel = "/index.html"
        elif rel.endswith("/"):
            rel += "index.html"

        root = DIST_DIR.resolve()
        candidate = (root / rel.lstrip("/")).resolve()
        try:
            candidate.relative_to(root)
        except ValueError:
            return self._not_found()

        if candidate.is_file():
            body = candidate.read_bytes()
            content_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
            if content_type.startswith("text/") or content_type in (
                "application/javascript",
                "application/json",
            ):
                content_type += "; charset=utf-8"
            cache = "no-store" if candidate.name == "index.html" else "public, max-age=60"
            return Response(
                200,
                "OK",
                Headers(
                    [
                        ("Content-Type", content_type),
                        ("Content-Length", str(len(body))),
                        ("Cache-Control", cache),
                    ]
                ),
                body,
            )

        # 与真实服务端一致：未知路径回退 index.html
        index = root / "index.html"
        if index.is_file():
            body = index.read_bytes()
            return Response(
                200,
                "OK",
                Headers(
                    [
                        ("Content-Type", "text/html; charset=utf-8"),
                        ("Content-Length", str(len(body))),
                        ("Cache-Control", "no-store"),
                    ]
                ),
                body,
            )
        return self._not_found()

    @staticmethod
    def _not_found() -> Response:
        body = b"not found"
        return Response(
            404,
            "Not Found",
            Headers([("Content-Type", "text/plain; charset=utf-8"), ("Content-Length", str(len(body)))]),
            body,
        )

    # ---------- HTTP 钩子 ----------

    async def process_request(self, connection, request):
        path = urlsplit(request.path).path or "/"
        if path == "/healthz":
            body = json.dumps({"ok": True}).encode()
            return Response(
                200,
                "OK",
                Headers([("Content-Type", "application/json"), ("Content-Length", str(len(body)))]),
                body,
            )
        if path.endswith("/ws/web"):
            return None  # 交给 WebSocket 握手
        return self.serve_static(path)

    # ---------- WebSocket ----------

    async def handler(self, connection) -> None:
        path = urlsplit(connection.request.path).path
        await self.log({"ev": "ws-open", "path": path})
        status = OFFLINE_STATUS if self.device_offline else ONLINE_STATUS
        await connection.send(json.dumps(status, ensure_ascii=False))

        view_on = False
        closed = asyncio.Event()
        ack_id = 0

        async def frame_loop() -> None:
            while not closed.is_set():
                if view_on and not self.device_offline:
                    try:
                        await connection.send(self.frame)
                    except ConnectionClosed:
                        return
                await asyncio.sleep(0.15)

        async def server_ping() -> None:
            # 验证浏览器会回 pong（PROTOCOL.md 第 3 节）
            await asyncio.sleep(1.0)
            try:
                await connection.send(json.dumps({"t": "ping"}))
            except ConnectionClosed:
                pass

        frame_task = asyncio.create_task(frame_loop())
        ping_task = asyncio.create_task(server_ping())
        try:
            async for raw in connection:
                if isinstance(raw, bytes):
                    await self.log({"ev": "binary-in", "size": len(raw)})
                    continue
                try:
                    message = json.loads(raw)
                except json.JSONDecodeError:
                    message = raw
                await self.log({"ev": "ws-in", "msg": message})
                if not isinstance(message, dict):
                    continue
                kind = message.get("t")
                if kind == "view":
                    view_on = bool(message.get("on"))
                    await self.log({"ev": "view", "on": view_on})
                elif kind == "cmd":
                    ack_id += 1
                    await connection.send(json.dumps({"t": "ack", "id": f"c{ack_id}"}))
                elif kind == "ping":
                    await connection.send(json.dumps({"t": "pong"}))
        except ConnectionClosed:
            pass
        finally:
            closed.set()
            frame_task.cancel()
            ping_task.cancel()
            await self.log({"ev": "ws-close", "path": path})


async def main() -> None:
    parser = argparse.ArgumentParser(description="前端自测假服务端")
    parser.add_argument("--port", type=int, default=18093)
    parser.add_argument("--log", type=str, default=None, help="JSONL 日志路径")
    parser.add_argument("--host", type=str, default="127.0.0.1")
    parser.add_argument("--device-offline", action="store_true", help="模拟手机端未连接（status.online=false）")
    args = parser.parse_args()

    if not (DIST_DIR / "index.html").is_file():
        print("缺少 web/dist/index.html，请先执行 npm run build", file=sys.stderr)
        raise SystemExit(2)

    log_path = Path(args.log).resolve() if args.log else None
    if log_path is not None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("", encoding="utf-8")

    server = MockServer(args.port, log_path, device_offline=args.device_offline)
    async with serve(
        server.handler,
        args.host,
        args.port,
        process_request=server.process_request,
        ping_interval=None,
        max_size=8 * 1024 * 1024,
    ):
        print(f"MOCK READY http://{args.host}:{args.port}/", flush=True)
        await asyncio.Future()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass

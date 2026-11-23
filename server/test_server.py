#!/usr/bin/env python3
"""server.py 的单元测试（只依赖标准库 + websockets 客户端）。

运行：
    python server/test_server.py

测试用假设备客户端 / 假浏览器客户端连真实的本机服务端（127.0.0.1 随机临时端口，
不占用 8090），覆盖 PROTOCOL.md 与 task-1 列出的验收项。
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Callable

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

# 支持两种运行方式：`python server/test_server.py` 与 `python -m unittest ...`
sys.path.insert(0, str(Path(__file__).resolve().parent))
from server import Config, ControlServer, normalize_path  # noqa: E402

DEVICE_KEY = "test-device-key"
# 测试客户端：不要自动重连、不要 WS 控制帧心跳、不要压缩，保证消息序列可预测。
CLIENT_KWARGS = dict(
    ping_interval=None,
    ping_timeout=None,
    close_timeout=2,
    compression=None,
    proxy=None,
    max_size=None,
    open_timeout=5,
)


class ServerTestBase(unittest.IsolatedAsyncioTestCase):
    """起一个独立的 ControlServer（随机端口），测试结束确保关闭并释放端口。"""

    static_files: dict[str, str] = {}
    device_key: str = DEVICE_KEY
    # 正常测试里把心跳调大到不会打扰消息断言
    heartbeat: dict[str, float] = {"ping_interval": 3600.0, "idle_timeout": 3600.0, "tick": 0.1}

    async def asyncSetUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="phone-control-test-"))
        for rel, content in self.static_files.items():
            target = self.tmpdir / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        self.server = ControlServer(
            Config(
                host="127.0.0.1",
                port=0,  # 系统分配临时端口，避免和 8090/18090 冲突
                device_key=self.device_key,
                static_dir=self.tmpdir,
                **self.heartbeat,
            )
        )
        await self.server.start()
        self.port = self.server.bound_port
        self._clients: list[Any] = []

    async def asyncTearDown(self) -> None:
        for ws in self._clients:
            try:
                await ws.close()
            except Exception:
                pass
        await self.server.stop()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    # -- 客户端连接辅助 ---------------------------------------------------

    def _track(self, ws):
        self._clients.append(ws)
        return ws

    async def connect_device(self, key: str = DEVICE_KEY, path: str = "/ws/device"):
        uri = f"ws://127.0.0.1:{self.port}{path}?key={key}"
        ws = await connect(uri, **CLIENT_KWARGS)
        return self._track(ws)

    async def connect_browser(self, path: str = "/ws/web"):
        ws = await connect(f"ws://127.0.0.1:{self.port}{path}", **CLIENT_KWARGS)
        return self._track(ws)

    # -- 消息断言辅助 -----------------------------------------------------

    async def recv_json(self, ws, expected_t: str | None = None, timeout: float = 2.0) -> dict[str, Any]:
        """读下一条文本帧并解析；跳过不匹配 expected_t 的文本帧，遇到二进制帧即失败。"""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                self.fail(f"等待服务器消息超时（t={expected_t!r}）")
            raw = await asyncio.wait_for(ws.recv(), remaining)
            if isinstance(raw, bytes):
                self.fail(f"期望文本帧，收到二进制帧（{len(raw)} bytes）")
            msg = json.loads(raw)
            if expected_t is None or msg.get("t") == expected_t:
                return msg

    async def recv_json_matching(self, ws, predicate: Callable[[dict[str, Any]], bool],
                                 timeout: float = 2.0) -> dict[str, Any]:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                self.fail("等待满足条件的服务器消息超时")
            raw = await asyncio.wait_for(ws.recv(), remaining)
            if isinstance(raw, bytes):
                continue
            msg = json.loads(raw)
            if predicate(msg):
                return msg

    async def recv_bytes(self, ws, timeout: float = 2.0) -> bytes:
        raw = await asyncio.wait_for(ws.recv(), timeout)
        if isinstance(raw, str):
            self.fail(f"期望二进制 JPEG 帧，收到文本帧: {raw!r}")
        return raw

    async def assert_no_message(self, ws, timeout: float = 0.25) -> None:
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout)
        except TimeoutError:
            return
        self.fail(f"不应收到消息，却收到: {raw!r}")

    async def wait_ws_closed(self, ws, timeout: float = 3.0) -> None:
        async def drain() -> None:
            try:
                while True:
                    await ws.recv()
            except ConnectionClosed:
                return

        await asyncio.wait_for(drain(), timeout)

    # -- 原始 HTTP 辅助 ---------------------------------------------------

    async def http_request(self, path: str, method: str = "GET") -> tuple[int, dict[str, str], bytes]:
        reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
        try:
            request = (
                f"{method} {path} HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{self.port}\r\n"
                "Connection: close\r\n"
                "\r\n"
            )
            writer.write(request.encode("ascii"))
            await writer.drain()
            data = await asyncio.wait_for(reader.read(), 5)
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

        head, _, body = data.partition(b"\r\n\r\n")
        self.assertTrue(head, f"空响应: {data!r}")
        lines = head.split(b"\r\n")
        status = int(lines[0].split(b" ")[1])
        headers: dict[str, str] = {}
        for line in lines[1:]:
            key, _, value = line.partition(b":")
            headers[key.decode("latin-1").strip().lower()] = value.decode("latin-1").strip()
        return status, headers, body


class StaticAndHealthTest(ServerTestBase):
    static_files = {
        "index.html": "<!doctype html><title>手机控制台</title><div id=app></div>",
        "assets/app.js": "console.log('control')",
        "assets/app.css": "body{background:#3a3f47}",
    }

    async def test_healthz(self) -> None:
        status, headers, body = await self.http_request("/healthz")
        self.assertEqual(status, 200)
        self.assertIn("application/json", headers["content-type"])
        self.assertEqual(json.loads(body), {"ok": True})
        self.assertEqual(headers.get("cache-control"), "no-store")

    async def test_root_serves_index(self) -> None:
        status, headers, body = await self.http_request("/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers["content-type"])
        self.assertIn(b"<title>", body)
        self.assertIn("no-store", headers.get("cache-control", ""))

    async def test_asset_content_type(self) -> None:
        status, headers, body = await self.http_request("/assets/app.js")
        self.assertEqual(status, 200)
        self.assertIn("javascript", headers["content-type"])
        self.assertIn(b"console.log", body)
        status, headers, _ = await self.http_request("/assets/app.css")
        self.assertEqual(status, 200)
        self.assertIn("text/css", headers["content-type"])

    async def test_unknown_path_falls_back_to_index(self) -> None:
        status, headers, body = await self.http_request("/control/some/deep/link")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers["content-type"])
        self.assertIn(b"<title>", body)

    async def test_directory_traversal_does_not_escape_root(self) -> None:
        status, _, body = await self.http_request("/../../PROTOCOL.md")
        self.assertEqual(status, 200)
        self.assertIn(b"<title>", body)  # 回退 index.html，绝不泄露仓库文件

    async def test_agent_prefix_stripped_for_http(self) -> None:
        status, _, body = await self.http_request("/agent/healthz")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"ok": True})

    async def test_head_request(self) -> None:
        status, headers, body = await self.http_request("/assets/app.js", method="HEAD")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"")
        self.assertEqual(int(headers["content-length"]), len("console.log('control')"))

    async def test_method_not_allowed(self) -> None:
        status, headers, _ = await self.http_request("/healthz", method="POST")
        self.assertEqual(status, 405)
        self.assertIn("GET", headers.get("allow", ""))

    async def test_websocket_still_works(self) -> None:
        """静态站点与 WS 同端口：HTTP 请求之后 WS 仍能握手。"""
        await self.http_request("/")
        browser = await self.connect_browser()
        status = await self.recv_json(browser, "status")
        self.assertFalse(status["online"])


class MissingStaticDirTest(ServerTestBase):
    """web/dist 还不存在时：页面 404，但 WS 通道必须正常。"""

    async def test_http_404_but_ws_ok(self) -> None:
        # 重新起一个 static_dir 指向不存在目录的实例
        missing_dir = self.tmpdir / "does-not-exist"
        server = ControlServer(
            Config(host="127.0.0.1", port=0, device_key=DEVICE_KEY, static_dir=missing_dir,
                   **ServerTestBase.heartbeat)
        )
        await server.start()
        self.addAsyncCleanup(server.stop)
        port = server.bound_port

        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"GET / HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
        await writer.drain()
        data = await asyncio.wait_for(reader.read(), 5)
        writer.close()
        self.assertTrue(data.startswith(b"HTTP/1.1 404"), data[:80])

        ws = await connect(f"ws://127.0.0.1:{port}/ws/web", **CLIENT_KWARGS)
        self._clients.append(ws)
        status = await asyncio.wait_for(ws.recv(), 2)
        self.assertEqual(json.loads(status)["t"], "status")

        device = await connect(f"ws://127.0.0.1:{port}/ws/device?key={DEVICE_KEY}", **CLIENT_KWARGS)
        self._clients.append(device)
        viewers = json.loads(await asyncio.wait_for(device.recv(), 2))
        self.assertEqual(viewers, {"t": "viewers", "n": 0})

    async def test_healthz_ok_with_missing_static(self) -> None:
        missing_dir = self.tmpdir / "does-not-exist"
        server = ControlServer(
            Config(host="127.0.0.1", port=0, device_key=DEVICE_KEY, static_dir=missing_dir,
                   **ServerTestBase.heartbeat)
        )
        await server.start()
        self.addAsyncCleanup(server.stop)
        port = server.bound_port
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"GET /healthz HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
        await writer.drain()
        data = await asyncio.wait_for(reader.read(), 5)
        writer.close()
        self.assertTrue(data.startswith(b"HTTP/1.1 200"), data[:80])
        self.assertIn(b'{"ok":true}', data)


class DeviceAuthTest(ServerTestBase):
    async def test_wrong_key_rejected_before_upgrade(self) -> None:
        with self.assertRaises(InvalidStatus) as ctx:
            await connect(
                f"ws://127.0.0.1:{self.port}/ws/device?key=wrong-key", **CLIENT_KWARGS
            )
        self.assertEqual(ctx.exception.response.status_code, 401)

    async def test_missing_key_rejected(self) -> None:
        with self.assertRaises(InvalidStatus) as ctx:
            await connect(f"ws://127.0.0.1:{self.port}/ws/device", **CLIENT_KWARGS)
        self.assertEqual(ctx.exception.response.status_code, 401)

    async def test_empty_configured_key_rejects_everything(self) -> None:
        server = ControlServer(
            Config(host="127.0.0.1", port=0, device_key="", static_dir=self.tmpdir,
                   **ServerTestBase.heartbeat)
        )
        await server.start()
        self.addAsyncCleanup(server.stop)
        port = server.bound_port
        with self.assertRaises(InvalidStatus) as ctx:
            await connect(f"ws://127.0.0.1:{port}/ws/device?key=", **CLIENT_KWARGS)
        self.assertEqual(ctx.exception.response.status_code, 401)

    async def test_correct_key_accepted_with_agent_prefix(self) -> None:
        device = await self.connect_device(path="/agent/ws/device")
        viewers = await self.recv_json(device, "viewers")
        self.assertEqual(viewers["n"], 0)


class StatusAndRelayTest(ServerTestBase):
    async def test_browser_gets_offline_status_first(self) -> None:
        browser = await self.connect_browser()
        status = await self.recv_json(browser, "status")
        self.assertEqual(
            status,
            {"t": "status", "online": False, "name": None, "w": None, "h": None,
             "accessibility": None},
        )

    async def test_hello_broadcast_to_browsers(self) -> None:
        browser = await self.connect_browser()
        await self.recv_json(browser, "status")

        device = await self.connect_device()
        await self.recv_json(device, "viewers")

        await device.send(json.dumps({
            "t": "hello", "name": "Pixel 7", "w": 1080, "h": 2400,
            "accessibility": True, "version": 1,
        }))
        status = await self.recv_json(browser, "status")
        self.assertEqual(status["online"], True)
        self.assertEqual(status["name"], "Pixel 7")
        self.assertEqual(status["w"], 1080)
        self.assertEqual(status["h"], 2400)
        self.assertIs(status["accessibility"], True)

    async def test_late_browser_gets_current_status(self) -> None:
        device = await self.connect_device()
        await self.recv_json(device, "viewers")
        await device.send(json.dumps({"t": "hello", "name": "Pixel", "w": 720, "h": 1280,
                                     "accessibility": False}))

        browser = await self.connect_browser()
        status = await self.recv_json(browser, "status")
        self.assertTrue(status["online"])
        self.assertEqual(status["name"], "Pixel")
        self.assertEqual(status["w"], 720)
        self.assertIs(status["accessibility"], False)

    async def test_device_disconnect_broadcasts_offline(self) -> None:
        browser = await self.connect_browser()
        await self.recv_json(browser, "status")

        device = await self.connect_device()
        await self.recv_json(device, "viewers")
        await device.send(json.dumps({"t": "hello", "name": "Pixel", "w": 1080, "h": 2400,
                                     "accessibility": True}))
        await self.recv_json(browser, "status")

        await device.close()
        status = await self.recv_json_matching(browser, lambda m: m.get("t") == "status")
        self.assertEqual(status["online"], False)
        self.assertIsNone(status["name"])

    async def test_viewer_receives_frames_and_last_frame_is_cached(self) -> None:
        device = await self.connect_device()
        await self.recv_json(device, "viewers")
        await device.send(json.dumps({"t": "hello", "name": "Pixel", "w": 1080, "h": 2400,
                                     "accessibility": True}))

        browser = await self.connect_browser()
        await self.recv_json(browser, "status")
        await browser.send(json.dumps({"t": "view", "on": True}))
        viewers = await self.recv_json(device, "viewers")
        self.assertEqual(viewers["n"], 1)

        jpeg = b"\xff\xd8\xff\xe0fake-jpeg-payload\xff\xd9"
        await device.send(jpeg)
        self.assertEqual(await self.recv_bytes(browser), jpeg)

        # 后开的浏览器 view on 后立即拿到缓存的最后一帧
        late = await self.connect_browser()
        await self.recv_json(late, "status")
        await late.send(json.dumps({"t": "view", "on": True}))
        self.assertEqual(await self.recv_bytes(late), jpeg)

    async def test_view_off_receives_no_frames(self) -> None:
        device = await self.connect_device()
        await self.recv_json(device, "viewers")
        browser = await self.connect_browser()
        await self.recv_json(browser, "status")

        # 浏览器没发 view:on，设备帧不应下发
        await device.send(b"\xff\xd8frame\xff\xd9")
        await self.assert_no_message(browser)

        # view 打开 -> 收到缓存帧
        await browser.send(json.dumps({"t": "view", "on": True}))
        self.assertEqual(await self.recv_bytes(browser), b"\xff\xd8frame\xff\xd9")

        # 关闭 view 后不再下发
        await browser.send(json.dumps({"t": "view", "on": False}))
        await self.recv_json(device, "viewers")  # n 变 0
        await device.send(b"\xff\xd8second\xff\xd9")
        await self.assert_no_message(browser)

    async def test_viewers_count_changes(self) -> None:
        device = await self.connect_device()
        await self.recv_json(device, "viewers")  # n=0（连上即推当前人数）

        b1 = await self.connect_browser()
        await self.recv_json(b1, "status")
        await b1.send(json.dumps({"t": "view", "on": True}))
        self.assertEqual((await self.recv_json(device, "viewers"))["n"], 1)

        b2 = await self.connect_browser()
        await self.recv_json(b2, "status")
        await b2.send(json.dumps({"t": "view", "on": True}))
        self.assertEqual((await self.recv_json(device, "viewers"))["n"], 2)

        await b1.send(json.dumps({"t": "view", "on": False}))
        self.assertEqual((await self.recv_json(device, "viewers"))["n"], 1)

        await b2.close()
        self.assertEqual((await self.recv_json(device, "viewers"))["n"], 0)

    async def test_command_roundtrip_ack_routes_to_origin_browser(self) -> None:
        device = await self.connect_device()
        await self.recv_json(device, "viewers")
        b1 = await self.connect_browser()
        await self.recv_json(b1, "status")
        b2 = await self.connect_browser()
        await self.recv_json(b2, "status")

        await b1.send(json.dumps({"t": "cmd", "action": "tap", "x": 0.5, "y": 0.3}))
        cmd = await self.recv_json(device, "cmd")
        self.assertTrue(isinstance(cmd["id"], str) and cmd["id"])
        self.assertEqual(cmd["action"], "tap")
        self.assertEqual((cmd["x"], cmd["y"]), (0.5, 0.3))

        await device.send(json.dumps({"t": "ack", "id": cmd["id"]}))
        ack = await self.recv_json(b1, "ack")
        self.assertEqual(ack["id"], cmd["id"])
        # 另一个浏览器不应收到别人的 ack
        await self.assert_no_message(b2)

    async def test_command_error_forwarded(self) -> None:
        device = await self.connect_device()
        await self.recv_json(device, "viewers")
        browser = await self.connect_browser()
        await self.recv_json(browser, "status")

        await browser.send(json.dumps({"t": "cmd", "action": "key", "name": "back"}))
        cmd = await self.recv_json(device, "cmd")
        await device.send(json.dumps({
            "t": "error", "id": cmd["id"], "message": "accessibility not enabled",
        }))
        err = await self.recv_json(browser, "error")
        self.assertEqual(err["id"], cmd["id"])
        self.assertEqual(err["message"], "accessibility not enabled")

    async def test_command_without_device_returns_error(self) -> None:
        browser = await self.connect_browser()
        await self.recv_json(browser, "status")
        await browser.send(json.dumps({"t": "cmd", "action": "tap", "x": 0.1, "y": 0.2}))
        err = await self.recv_json(browser, "error")
        self.assertTrue(err["id"])
        self.assertIn("offline", err["message"])

    async def test_client_ping_gets_pong(self) -> None:
        browser = await self.connect_browser()
        await self.recv_json(browser, "status")
        await browser.send(json.dumps({"t": "ping"}))
        pong = await self.recv_json(browser, "pong")
        self.assertEqual(pong, {"t": "pong"})

    async def test_device_replacement_kicks_old_device(self) -> None:
        old = await self.connect_device()
        await self.recv_json(old, "viewers")
        await old.send(json.dumps({"t": "hello", "name": "Old Phone", "w": 1080, "h": 2400,
                                   "accessibility": True}))

        browser = await self.connect_browser()
        status = await self.recv_json(browser, "status")
        self.assertEqual(status["name"], "Old Phone")

        new = await self.connect_device()
        await self.recv_json(new, "viewers")

        bye = await self.recv_json(old, "bye")
        self.assertEqual(bye["reason"], "replaced")
        await self.wait_ws_closed(old)

        await new.send(json.dumps({"t": "hello", "name": "New Phone", "w": 1440, "h": 3120,
                                   "accessibility": False}))
        status = await self.recv_json_matching(
            browser, lambda m: m.get("t") == "status" and m.get("name") == "New Phone"
        )
        self.assertTrue(status["online"])
        self.assertEqual(status["w"], 1440)

    async def test_unknown_websocket_route_rejected(self) -> None:
        with self.assertRaises(InvalidStatus) as ctx:
            await connect(f"ws://127.0.0.1:{self.port}/ws/nope", **CLIENT_KWARGS)
        self.assertEqual(ctx.exception.response.status_code, 404)


class HeartbeatTest(ServerTestBase):
    heartbeat = {"ping_interval": 0.2, "idle_timeout": 0.8, "tick": 0.05}

    async def test_server_ping_and_idle_disconnect(self) -> None:
        browser = await self.connect_browser()
        await self.recv_json(browser, "status")

        # 服务端主动发文本 ping；客户端回 pong 可保活
        ping = await self.recv_json(browser, "ping", timeout=2.0)
        self.assertEqual(ping, {"t": "ping"})
        await browser.send(json.dumps({"t": "pong"}))
        await browser.send(json.dumps({"t": "ping"}))
        pong = await self.recv_json(browser, "pong", timeout=2.0)
        self.assertEqual(pong, {"t": "pong"})

        # 之后不再回应任何帧：超过 idle_timeout 应被服务端断开
        await self.wait_ws_closed(browser, timeout=3.0)


class NormalizePathTest(unittest.TestCase):
    def test_normalize(self) -> None:
        self.assertEqual(normalize_path("/"), "/")
        self.assertEqual(normalize_path("/?x=1"), "/")
        self.assertEqual(normalize_path("/agent/ws/device?key=k"), "/ws/device")
        self.assertEqual(normalize_path("/agent"), "/")
        self.assertEqual(normalize_path("/agent/healthz"), "/healthz")
        self.assertEqual(normalize_path("/ws/web"), "/ws/web")
        self.assertEqual(normalize_path("/a%20b/c.js"), "/a b/c.js")


if __name__ == "__main__":
    unittest.main(verbosity=2)

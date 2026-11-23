"""假设备客户端：按 PROTOCOL.md 模拟手机端，用于服务端/前端端到端联调。

用法：
    python tools/fake_device.py --url ws://127.0.0.1:18090/ws/device --key testkey --fps 5 --seconds 600

效果：连上服务端后发 hello，收到 viewers>0 就开始推 JPEG 帧（画面是时间戳 + 当前指令回显），
收到 cmd 打印并回 ack（text 指令把内容画进画面），用于人工/自动化验证整条链路。
"""
from __future__ import annotations

import argparse
import asyncio
import io
import json
import sys
import time

try:
    import websockets
except ImportError:  # pragma: no cover
    print("需要 websockets: pip install websockets", file=sys.stderr)
    raise

try:
    from PIL import Image, ImageDraw
except ImportError:  # pragma: no cover
    Image = None

W, H = 720, 1280


class FakeDevice:
    def __init__(self, fps: float = 5.0, accessibility: bool = True):
        self.fps = fps
        self.accessibility = accessibility
        self.viewers = 0
        self.last_cmd = "(none)"
        self.last_text = ""
        self.commands: list[dict] = []
        self.send_lock = asyncio.Lock()

    def render(self) -> bytes:
        """生成一帧 JPEG；PIL 不可用时用纯色最小 JPEG。"""
        if Image is None:
            return _tiny_jpeg()
        img = Image.new("RGB", (W, H), (58, 63, 71))
        d = ImageDraw.Draw(img)
        d.rectangle([0, 0, W, 160], fill=(242, 243, 245))
        d.text((24, 30), "FAKE PHONE", fill=(58, 63, 71))
        d.text((24, 70), time.strftime("%H:%M:%S"), fill=(58, 63, 71))
        d.text((24, 110), f"viewers={self.viewers}", fill=(58, 63, 71))
        d.text((24, 220), f"last cmd: {self.last_cmd}", fill=(242, 243, 245))
        d.text((24, 260), f"text: {self.last_text}", fill=(242, 243, 245))
        for i in range(12):
            y = 340 + i * 70
            d.line([24, y, W - 24, y], fill=(120, 126, 134), width=2)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=60)
        return buf.getvalue()

    async def pump_frames(self, ws):
        """viewers>0 时按 fps 推帧。"""
        interval = 1.0 / self.fps
        while True:
            if self.viewers > 0:
                frame = self.render()
                try:
                    await ws.send(frame)
                except Exception:
                    return
                await asyncio.sleep(interval)
            else:
                await asyncio.sleep(0.1)

    async def run(self, url: str, seconds: float):
        async with websockets.connect(url, max_size=8 * 1024 * 1024) as ws:
            hello = {"t": "hello", "name": "FakePhone", "w": W, "h": H, "accessibility": self.accessibility, "version": 1}
            await ws.send(json.dumps(hello))
            print(f"[device] connected, hello sent -> {url}", flush=True)
            pump = asyncio.create_task(self.pump_frames(ws))
            deadline = time.time() + seconds
            try:
                while time.time() < deadline:
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=max(1.0, deadline - time.time()))
                    except asyncio.TimeoutError:
                        break
                    try:
                        msg = json.loads(raw)
                    except (TypeError, ValueError):
                        continue  # 二进制帧不走这里
                    t = msg.get("t")
                    if t == "viewers":
                        self.viewers = int(msg.get("n", 0))
                        print(f"[device] viewers={self.viewers}", flush=True)
                    elif t == "cmd":
                        action = msg.get("action")
                        self.commands.append(msg)
                        self.last_cmd = f"{action} {json.dumps({k: v for k, v in msg.items() if k not in ('t', 'id', 'action')}, ensure_ascii=False)}"
                        if action == "text":
                            self.last_text = msg.get("text", "")[:40]
                        print(f"[device] CMD {json.dumps(msg, ensure_ascii=False)}", flush=True)
                        reply = {"t": "ack", "id": msg.get("id")}
                        if not msg.get("id"):
                            reply = {"t": "error", "message": "missing id"}
                        await ws.send(json.dumps(reply))
                    elif t == "ping":
                        await ws.send(json.dumps({"t": "pong"}))
                    elif t == "bye":
                        print("[device] bye from server", flush=True)
                        break
            finally:
                pump.cancel()
                print(f"[device] commands total={len(self.commands)}", flush=True)


def _tiny_jpeg() -> bytes:
    """不依赖 PIL 的 1x1 灰色 JPEG。"""
    return bytes.fromhex(
        "ffd8ffe000104a46494600010100000100010000ffdb004300"
        "080606070606080707070909080a0c140d0c0b0b0c1912130f141d1a1f1e1d1a1c1c20242e2720222c231c1c2837"
        "292c30313434341f27393d38323c2e333432ffc0000b080001000101011100ffc4001f000001050101010101010000"
        "00000000000102030405060708090a0bffc400b5100002010303020403050504040000017d01020300041105122131"
        "410613516107227114328191a1082342b1c11552d1f02433627282090a161718191a25262728292a343536373839"
        "3a434445464748494a535455565758595a636465666768696a737475767778797a838485868788898a9293949596"
        "9798999aa2a3a4a5a6a7a8a9aab2b3b4b5b6b7b8b9bac2c3c4c5c6c7c8c9cad2d3d4d5d6d7d8d9dae1e2e3e4e5e6e7"
        "e8e9eaf1f2f3f4f5f6f7f8f9faffda0008010100003f00fbfa8affd9"
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="ws://127.0.0.1:18090/ws/device")
    ap.add_argument("--key", default="testkey")
    ap.add_argument("--fps", type=float, default=5.0)
    ap.add_argument("--seconds", type=float, default=600)
    ap.add_argument("--no-a11y", action="store_true", help="模拟无障碍未开启的设备")
    args = ap.parse_args()
    url = f"{args.url}?key={args.key}" if "?" not in args.url else args.url
    try:
        asyncio.run(FakeDevice(args.fps, accessibility=not args.no_a11y).run(url, args.seconds))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())

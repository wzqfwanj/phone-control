"""浏览器侧探针：模拟电脑浏览器连 server.py 的 /ws/web，验证设备通道整链路（dev only）。

做的事情：
  1. 连 ws://<host>:<port>/ws/web
  2. 发 {"t":"view","on":true} 表示开始观看（服务端据此给设备发 viewers n=1）
  3. 收 status（设备上线信息）/ 二进制 JPEG 帧 / ack / pong
  4. 先发一条 key:notifications（下拉通知栏产生画面变化，顺便测全局动作 ack），
     收到帧后发一条 tap，检查能否收到对应 {"t":"ack","id":...}
  5. 结束时发 key:back 收起通知栏；打印汇总，PASS/FAIL 用退出码表示

用法：python android/tools/web_probe.py [port] [host]
注意：MediaProjection 只在画面变化时出帧，所以探针必须制造一点 UI 变化才能看到连续帧。
"""

import asyncio
import json
import sys

import websockets


async def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8090
    host = sys.argv[2] if len(sys.argv) > 2 else "127.0.0.1"
    url = "ws://%s:%d/ws/web" % (host, port)
    print("connecting %s" % url, flush=True)

    status = None
    frames = 0
    frame_sizes = []
    first_frame_is_jpeg = None
    sent_key = False
    sent_tap = False
    acks = []
    errors = []
    notifications_sent_at = None

    async with websockets.connect(url, max_size=8 * 1024 * 1024) as ws:
        await ws.send(json.dumps({"t": "view", "on": True}))
        print("SENT view:on", flush=True)
        loop = asyncio.get_event_loop()
        deadline = loop.time() + 30
        while loop.time() < deadline:
            try:
                message = await asyncio.wait_for(ws.recv(), timeout=max(0.5, deadline - loop.time()))
            except asyncio.TimeoutError:
                break
            if isinstance(message, bytes):
                frames += 1
                frame_sizes.append(len(message))
                if frames == 1:
                    is_jpeg = message[:2] == b"\xff\xd8" and message[-2:] == b"\xff\xd9"
                    first_frame_is_jpeg = is_jpeg
                    print("FRAME #1 size=%d jpeg=%s headers=%s"
                          % (len(message), is_jpeg, message[:4].hex()), flush=True)
                    if not is_jpeg:
                        errors.append("first frame is not a JPEG")
                if frames == 3 and not sent_tap:
                    sent_tap = True
                    print("SENT cmd tap 0.5,0.5", flush=True)
                    await ws.send(json.dumps({"t": "cmd", "action": "tap", "x": 0.5, "y": 0.5}))
                continue
            print("TEXT %s" % message, flush=True)
            try:
                payload = json.loads(message)
            except Exception:
                continue
            kind = payload.get("t")
            if kind == "status":
                status = payload
                if payload.get("online") and payload.get("accessibility") and not sent_key:
                    sent_key = True
                    notifications_sent_at = loop.time()
                    print("SENT cmd key notifications", flush=True)
                    await ws.send(json.dumps({"t": "cmd", "action": "key", "name": "notifications"}))
            elif kind == "ack":
                acks.append(payload.get("id"))
                print("GOT ack id=%s (total %d)" % (payload.get("id"), len(acks)), flush=True)
            elif kind == "error":
                errors.append("device error: %s (%s)" % (payload.get("message"), payload.get("id")))
            elif kind == "bye":
                errors.append("bye: %s" % payload.get("reason"))
        try:
            await ws.send(json.dumps({"t": "cmd", "action": "key", "name": "back"}))
            await ws.send(json.dumps({"t": "view", "on": False}))
        except Exception:
            pass

    meaningful_frames = [size for size in frame_sizes if size > 2000]
    ok = (
        status is not None
        and status.get("online") is True
        and status.get("accessibility") is True
        and first_frame_is_jpeg is True
        and len(meaningful_frames) >= 2
        and len(acks) >= 2
        and not errors
    )
    print("RESULT %s online=%s accessibility=%s frames=%d (jpeg>2KB:%d) first_frame=%sB acks=%d errors=%s"
          % ("PASS" if ok else "FAIL",
             status.get("online") if status else None,
             status.get("accessibility") if status else None,
             frames, len(meaningful_frames),
             frame_sizes[0] if frame_sizes else 0,
             len(acks), errors), flush=True)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    asyncio.run(main())

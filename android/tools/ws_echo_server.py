"""最小 WebSocket 服务端，只给 android/tools 下的 WsClient 冒烟自测用（dev only）。

不参与部署，也不替代 server/server.py。行为：
  - 接受任意连接，连上立刻发一条 {"t":"viewers","n":1}
  - 收到文本 {"t":"ping"} → 回 {"t":"pong"}
  - 收到 hello / 二进制帧 → 打印日志
  - 连接期间每 10s 发一条协议心跳 {"t":"ping"}

用法：python android/tools/ws_echo_server.py [port]
依赖：websockets（本机已装，server/requirements.txt 也用它）。
"""

import asyncio
import json
import sys

import websockets


async def handler(websocket):
    path = ""
    try:
        path = websocket.request.path
    except Exception:
        pass
    print("CONNECT %s" % path, flush=True)
    await websocket.send(json.dumps({"t": "viewers", "n": 1}))
    try:
        async for message in websocket:
            if isinstance(message, bytes):
                print("BINARY %d" % len(message), flush=True)
                continue
            print("TEXT %s" % message, flush=True)
            try:
                payload = json.loads(message)
            except Exception:
                continue
            if payload.get("t") == "ping":
                await websocket.send(json.dumps({"t": "pong"}))
            elif payload.get("t") == "hello":
                # 顺便下发一条指令，方便观察 App 侧 ack 路径（本脚本自己不校验）
                await websocket.send(json.dumps({
                    "t": "cmd", "id": "smoke-1", "action": "tap", "x": 0.5, "y": 0.5,
                }))
    except Exception as exc:  # 客户端断开等
        print("RECV ERROR %r" % (exc,), flush=True)
    finally:
        print("DISCONNECT %s" % path, flush=True)


async def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8099
    async with websockets.serve(handler, "127.0.0.1", port):
        print("listening ws://127.0.0.1:%d/agent/ws/device?key=test" % port, flush=True)
        await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())

"""浏览器侧探针：按 PROTOCOL.md 第 5 节模拟网页端，验证「看画面 + 发指令」整条链路。

用法：
    python tools/browser_probe.py --url ws://127.0.0.1:8090/ws/web --http-user u --http-pass p \
        --frames 3 --out /tmp/frame.jpg --key back

用途：
  * 在 dev 主机上直连 127.0.0.1:8090/ws/web 验证服务端+设备
  * 在 aliyun 上连 wss://127.0.0.1/control/ws/web（Host: wkeeli.cn）验证 Caddy 公网入口
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import ssl
import sys
import urllib.parse

import websockets


def basic_header(user: str, password: str) -> dict[str, str]:
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


async def run(args) -> int:
    headers = {}
    if args.basic and args.http_user:
        headers.update(basic_header(args.http_user, args.http_pass))
    if args.host_header:
        headers["Host"] = args.host_header
    ssl_ctx = None
    if args.url.startswith("wss://"):
        ssl_ctx = ssl.create_default_context()
        if args.insecure:
            ssl_ctx.check_hostname = False
            ssl_ctx.verify_mode = ssl.CERT_NONE

    frames = 0
    acks: list[dict] = []
    status: dict | None = None
    async with websockets.connect(args.url, additional_headers=headers, ssl=ssl_ctx,
                                  max_size=8 * 1024 * 1024) as ws:
        print(f"[probe] connected {args.url}", flush=True)
        await ws.send(json.dumps({"t": "view", "on": True}))
        if args.key:
            await ws.send(json.dumps({"t": "cmd", "action": "key", "name": args.key}))
        if args.tap:
            x, y = (float(v) for v in args.tap.split(","))
            await ws.send(json.dumps({"t": "cmd", "action": "tap", "x": x, "y": y}))
        if args.swipe:
            x1, y1, x2, y2 = (float(v) for v in args.swipe.split(","))
            await ws.send(json.dumps({"t": "cmd", "action": "swipe", "x1": x1, "y1": y1,
                                      "x2": x2, "y2": y2, "ms": 300}))
        deadline = asyncio.get_event_loop().time() + args.seconds
        last_binary = None
        while asyncio.get_event_loop().time() < deadline:
            remaining = max(0.2, deadline - asyncio.get_event_loop().time())
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=remaining)
            except asyncio.TimeoutError:
                break
            if isinstance(msg, bytes):
                frames += 1
                last_binary = msg
                if frames == 1:
                    print(f"[probe] first frame {len(msg)} bytes jpeg={msg[:3].hex()}", flush=True)
            else:
                try:
                    data = json.loads(msg)
                except ValueError:
                    continue
                t = data.get("t")
                if t == "status":
                    status = data
                    print(f"[probe] status {data}", flush=True)
                elif t in ("ack", "error"):
                    acks.append(data)
                    print(f"[probe] {t} {data}", flush=True)
                elif t == "ping":
                    await ws.send(json.dumps({"t": "pong"}))
                if frames >= args.frames and acks and status is not None:
                    break

    if last_binary and args.out:
        with open(args.out, "wb") as f:
            f.write(last_binary)
        print(f"[probe] saved frame -> {args.out}", flush=True)
    print(json.dumps({"frames": frames, "status": status, "acks": acks}, ensure_ascii=False))
    ok = frames >= 1 and status is not None and bool(status.get("online")) and bool(status.get("accessibility"))
    if args.key or args.tap or args.swipe:
        ok = ok and any(a.get("t") == "ack" for a in acks)
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--seconds", type=float, default=12)
    ap.add_argument("--frames", type=int, default=3)
    ap.add_argument("--out", default="")
    ap.add_argument("--key", default="")
    ap.add_argument("--tap", default="")
    ap.add_argument("--swipe", default="")
    ap.add_argument("--basic", action="store_true")
    ap.add_argument("--http-user", default="")
    ap.add_argument("--http-pass", default="")
    ap.add_argument("--host-header", default="")
    ap.add_argument("--insecure", action="store_true")
    args = ap.parse_args()
    try:
        return asyncio.run(run(args))
    except Exception as exc:  # noqa: BLE001
        print(f"[probe] FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())

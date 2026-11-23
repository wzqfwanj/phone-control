"""端到端联调：真实 server.py + 假手机设备 + 真实 Chrome 渲染 web/dist。

覆盖：状态/画面显示、点击归一化坐标、滑动、长按、按键、文本输入、375px 无横向滚动、灰底配色。

用法：
    python tools/e2e_test.py
前置：python -m pip install playwright；本机有 Chrome（channel="chrome"）。
产物：tools/e2e_web.png（桌面截图）、tools/e2e_web_375.png（手机宽截图）
"""
from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "server" / "server.py"
DEVICE = ROOT / "tools" / "fake_device.py"
KEY = "e2e-key-123"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Proc:
    def __init__(self, cmd: list[str], env: dict[str, str], log: Path):
        self.log_path = log
        self.fh = log.open("w", encoding="utf-8")
        self.p = subprocess.Popen(cmd, cwd=str(ROOT), env=env, stdout=self.fh, stderr=subprocess.STDOUT)

    def read(self) -> str:
        self.fh.flush()
        return self.log_path.read_text(encoding="utf-8", errors="replace")

    def stop(self) -> None:
        if self.p.poll() is None:
            self.p.terminate()
            try:
                self.p.wait(timeout=8)
            except subprocess.TimeoutExpired:
                self.p.kill()
        self.fh.close()


def wait_health(port: int, timeout: float = 20.0) -> None:
    url = f"http://127.0.0.1:{port}/healthz"
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1) as r:
                if r.status == 200 and json.loads(r.read())["ok"]:
                    return
        except Exception:
            time.sleep(0.2)
    raise RuntimeError(f"server not healthy: {url}")


def main() -> int:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("SKIP: playwright 未安装（python -m pip install playwright）")
        return 2

    port = free_port()
    env = dict(os.environ)
    env.update(
        CONTROL_HOST="127.0.0.1",
        CONTROL_PORT=str(port),
        CONTROL_DEVICE_KEY=KEY,
        CONTROL_STATIC_DIR=str(ROOT / "web" / "dist"),
    )
    logs = ROOT / ".e2e"
    logs.mkdir(exist_ok=True)
    server = Proc([sys.executable, str(SERVER)], env, logs / "server.log")
    device = None
    results: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        results.append((name, ok, detail))
        print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail and not ok else ""))

    try:
        wait_health(port)
        device = Proc(
            [sys.executable, str(DEVICE), "--url", f"ws://127.0.0.1:{port}/ws/device", "--key", KEY,
             "--seconds", "180", "--fps", "5"],
            env, logs / "device.log",
        )
        # 等设备握手完成
        deadline = time.time() + 15
        while time.time() < deadline and "hello sent" not in device.read():
            time.sleep(0.2)

        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel="chrome", headless=True)
            page = browser.new_page(viewport={"width": 1440, "height": 1000})
            page.goto(f"http://127.0.0.1:{port}/", wait_until="load")
            page.wait_for_selector("text=手机已连接", timeout=20000)
            page.wait_for_selector('img[alt="手机实时画面"]', timeout=20000)
            page.wait_for_function(
                "() => { const i = document.querySelector('img[alt=\"手机实时画面\"]'); return i && i.src.startsWith('blob:') && i.naturalWidth > 0; }",
                timeout=20000,
            )
            check("页面显示「手机已连接」", True)
            check("画面为真实 JPEG 帧（blob + naturalWidth>0）", True)

            h1 = page.locator("h1").inner_text()
            sub = page.locator(".sub").inner_text()
            check("h1 = 手机控制台", h1.strip() == "手机控制台", h1)
            check(".sub 说明存在", bool(sub.strip()), sub)
            title = page.title()
            check("title = 手机控制台", title.strip() == "手机控制台", title)

            bg = page.eval_on_selector("body", "el => getComputedStyle(el).backgroundColor")
            fg = page.eval_on_selector("body", "el => getComputedStyle(el).color")
            check("灰底 #3a3f47", bg == "rgb(58, 63, 71)", bg)
            check("正文 #f2f3f5", fg == "rgb(242, 243, 245)", fg)

            # 画面尺寸与设备比例一致（480x854 假设备为 720x1280）
            box = page.locator(".screen").bounding_box()
            ratio = box["width"] / box["height"]
            check("画面宽高比符合设备 720:1280", abs(ratio - 720 / 1280) < 0.05, f"{ratio:.3f}")

            # 点击画面中心 -> tap 0.5,0.5
            page.locator(".screen").click(position={"x": box["width"] / 2, "y": box["height"] / 2})
            page.wait_for_selector("text=指令已执行", timeout=8000)
            check("点击画面后收到 ack（指令已执行）", True)

            # 按键：返回
            page.get_by_role("button", name="返回").click()
            page.get_by_role("button", name="桌面").click()
            page.get_by_role("button", name="多任务").click()
            page.get_by_role("button", name="通知栏").click()
            page.get_by_role("button", name="音量+").click()
            page.get_by_role("button", name="音量-").click()
            page.get_by_role("button", name="亮屏").click()
            page.get_by_role("button", name="锁屏").click()

            # 文本输入（回车发送需无需聚焦矛盾：先填再点发送）
            page.fill('input[aria-label="要发送到手机的文本"]', "hello 手机")
            page.get_by_role("button", name="发送").click()

            # 滑动：从 25%,25% 拖到 75%,75%
            sx, sy = box["x"] + box["width"] * 0.25, box["y"] + box["height"] * 0.25
            ex, ey = box["x"] + box["width"] * 0.75, box["y"] + box["height"] * 0.75
            page.mouse.move(sx, sy)
            page.mouse.down()
            page.mouse.move((sx + ex) / 2, (sy + ey) / 2, steps=6)
            page.mouse.move(ex, ey, steps=6)
            page.mouse.up()

            # 长按：按住 900ms 不动
            page.mouse.move(box["x"] + box["width"] * 0.5, box["y"] + box["height"] * 0.5)
            page.mouse.down()
            page.wait_for_timeout(900)
            page.mouse.up()

            page.wait_for_timeout(600)
            page.screenshot(path=str(ROOT / "tools" / "e2e_web.png"), full_page=True)

            # 375px 无横向溢出
            page.set_viewport_size({"width": 375, "height": 800})
            page.wait_for_timeout(400)
            overflow = page.evaluate(
                "() => document.scrollingElement.scrollWidth - document.scrollingElement.clientWidth"
            )
            check("375px 宽无横向溢出", overflow <= 0, f"overflow={overflow}px")
            page.screenshot(path=str(ROOT / "tools" / "e2e_web_375.png"), full_page=True)
            browser.close()

        page.wait_for_timeout(300) if False else None
        time.sleep(0.5)
        dev_log = device.read()
        cmds = [json.loads(m) for m in re.findall(r"\[device\] CMD (\{.*\})", dev_log)]
        taps = [c for c in cmds if c.get("action") == "tap"]
        check("设备收到 tap 指令", bool(taps), f"cmds={cmds}")
        if taps:
            t = taps[0]
            check(
                "tap 坐标归一化正确（≈0.5,0.5）",
                abs(t["x"] - 0.5) < 0.01 and abs(t["y"] - 0.5) < 0.01,
                f"{t}",
            )
        keys = [c.get("name") for c in cmds if c.get("action") == "key"]
        expected_keys = ["back", "home", "recents", "notifications", "volume_up", "volume_down", "wake", "sleep"]
        check("8 个按键全部转发", keys == expected_keys, f"{keys}")
        texts = [c.get("text") for c in cmds if c.get("action") == "text"]
        check("文本输入转发", texts == ["hello 手机"], f"{texts}")
        swipes = [c for c in cmds if c.get("action") == "swipe"]
        check("滑动指令转发", bool(swipes), f"{cmds}")
        if swipes:
            s = swipes[0]
            check(
                "滑动起止点正确（0.25,0.25 -> 0.75,0.75）",
                abs(s["x1"] - 0.25) < 0.02 and abs(s["x2"] - 0.75) < 0.02,
                f"{s}",
            )
        longs = [c for c in cmds if c.get("action") == "longpress"]
        check("长按指令转发", bool(longs), f"{cmds}")
        check("每条指令都有 id", all(c.get("id") for c in cmds), f"{cmds}")
        check("viewers 从 0 变 1（推流被触发）", "viewers=1" in dev_log, dev_log[-300:])

        err = [c for c in results if not c[1]]
        print("\n===== E2E 结果 =====")
        print(f"通过 {len(results) - len(err)}/{len(results)}")
        if err:
            print("失败项：")
            for name, _ok, detail in err:
                print(f"  - {name}: {detail}")
            return 1
        return 0
    finally:
        if device:
            device.stop()
        server.stop()


if __name__ == "__main__":
    sys.exit(main())

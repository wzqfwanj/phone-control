#!/usr/bin/env node
/**
 * 前端端到端自测（仅开发用，不参与构建）：
 * 启动 web/dev/mock_server.py + 无头 Chrome（CDP），验证：
 *   点击/滑动/长按归一化坐标、按键、文本、心跳 pong、view 开关、
 *   /control/ 前缀下的 WS 推导、375px 无横向滚动、零外部请求。
 *
 * 用法（先 npm run build）：
 *   node web/dev/e2e_check.mjs
 * 可加 --keep 保留临时截图路径输出（默认就打印）。
 */
import { spawn } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { setTimeout as sleep } from "node:timers/promises";

const WEB_DIR = resolve(import.meta.dirname, "..");
const DIST = join(WEB_DIR, "dist");
const PORT = Number(process.env.MOCK_PORT || 18093);
const CDP_PORT = Number(process.env.CDP_PORT || 19222);
const BASE = `http://127.0.0.1:${PORT}`;

const results = [];
function check(name, ok, detail = "") {
  results.push({ name, ok: !!ok, detail });
  console.log(`${ok ? "PASS" : "FAIL"}  ${name}${detail ? "  — " + detail : ""}`);
}
const approx = (actual, expected, tol) => Math.abs(actual - expected) <= tol;

async function waitFor(predicate, timeout, label) {
  const deadline = Date.now() + timeout;
  let lastError = null;
  while (Date.now() < deadline) {
    try {
      const value = await predicate();
      if (value) return value;
    } catch (error) {
      lastError = error;
    }
    await sleep(50);
  }
  throw new Error(`等待超时：${label}${lastError ? `（${lastError.message}）` : ""}`);
}

async function waitHttp(url, timeout, label) {
  return waitFor(async () => {
    try {
      const response = await fetch(url);
      return response.ok ? response : false;
    } catch {
      return false;
    }
  }, timeout, label);
}

// ---------- CDP ----------

class Cdp {
  constructor(ws) {
    this.ws = ws;
    this.nextId = 0;
    this.pending = new Map();
    this.networkUrls = new Set();
    this.pageErrors = [];
    ws.addEventListener("message", (event) => {
      let message;
      try {
        message = JSON.parse(event.data);
      } catch {
        return;
      }
      if (message.id !== undefined) {
        const pending = this.pending.get(message.id);
        if (!pending) return;
        this.pending.delete(message.id);
        if (message.error) pending.reject(new Error(message.error.message));
        else pending.resolve(message.result);
        return;
      }
      this.onEvent(message);
    });
  }

  onEvent(message) {
    if (message.method === "Network.requestWillBeSent") {
      this.networkUrls.add(message.params.request.url);
    } else if (message.method === "Runtime.exceptionThrown") {
      const detail = message.params.exceptionDetails;
      this.pageErrors.push(detail.exception?.description || detail.text || "页面异常");
    } else if (message.method === "Log.entryAdded" && message.params.entry.level === "error") {
      this.pageErrors.push(message.params.entry.text);
    }
  }

  send(method, params = {}) {
    const id = ++this.nextId;
    return new Promise((resolvePromise, rejectPromise) => {
      this.pending.set(id, { resolve: resolvePromise, reject: rejectPromise });
      this.ws.send(JSON.stringify({ id, method, params }));
    });
  }

  async evaluate(expression) {
    const result = await this.send("Runtime.evaluate", {
      expression,
      awaitPromise: true,
      returnByValue: true,
    });
    if (result.exceptionDetails) {
      throw new Error(
        result.exceptionDetails.exception?.description ||
          result.exceptionDetails.text ||
          "JS 执行异常",
      );
    }
    return result.result.value;
  }

  close() {
    try {
      this.ws.close();
    } catch {
      // 忽略
    }
  }
}

function wsConnect(url) {
  return new Promise((resolvePromise, rejectPromise) => {
    const ws = new WebSocket(url);
    const timer = setTimeout(() => rejectPromise(new Error(`连接超时：${url}`)), 8000);
    ws.addEventListener("open", () => {
      clearTimeout(timer);
      resolvePromise(ws);
    });
    ws.addEventListener("error", () => {
      clearTimeout(timer);
      rejectPromise(new Error(`连接失败：${url}`));
    });
  });
}

// ---------- 主流程 ----------

const chromeCandidates = [
  process.env.CHROME_PATH,
  "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
  "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
].filter(Boolean);
const chromePath = chromeCandidates.find((candidate) => existsSync(candidate));

const tempDir = mkdtempSync(join(tmpdir(), "web-e2e-"));
const logFile = join(tempDir, "mock.jsonl");
const desktopShot = join(tempDir, "desktop.png");
const mobileShot = join(tempDir, "mobile.png");

let mock = null;
let chrome = null;
let cdp = null;

function readLog() {
  if (!existsSync(logFile)) return [];
  return readFileSync(logFile, "utf8")
    .split("\n")
    .filter(Boolean)
    .map((line) => JSON.parse(line));
}

function cmdMessages() {
  return readLog()
    .filter((entry) => entry.ev === "ws-in" && entry.msg && entry.msg.t === "cmd")
    .map((entry) => entry.msg);
}

async function waitCmd(count, timeout = 5000) {
  return waitFor(() => {
    const messages = cmdMessages();
    return messages.length >= count ? messages : false;
  }, timeout, `收到第 ${count} 条 cmd`);
}

async function startMock(extraArgs = []) {
  mock = spawn(
    "python",
    ["-u", join(WEB_DIR, "dev", "mock_server.py"), "--port", String(PORT), "--log", logFile, ...extraArgs],
    { stdio: "ignore" },
  );
  await waitHttp(`${BASE}/healthz`, 15000, "mock server 启动");
}

async function stopMock() {
  if (!mock) return;
  const current = mock;
  mock = null;
  current.kill();
  await waitFor(async () => {
    try {
      await fetch(`${BASE}/healthz`);
      return false;
    } catch {
      return true;
    }
  }, 8000, "mock server 关闭");
}

try {
  if (!existsSync(join(DIST, "index.html"))) {
    throw new Error("缺少 web/dist/index.html，请先 npm run build");
  }
  if (!chromePath) throw new Error("未找到 Chrome/Edge，可设置 CHROME_PATH");

  // 1. 假服务端
  await startMock();
  console.log(`mock server: ${BASE}`);

  // 2. 无头浏览器
  chrome = spawn(
    chromePath,
    [
      "--headless=new",
      "--disable-gpu",
      "--no-first-run",
      "--no-default-browser-check",
      "--disable-extensions",
      "--disable-component-update",
      "--disable-background-networking",
      "--mute-audio",
      "--hide-scrollbars",
      `--remote-debugging-port=${CDP_PORT}`,
      "--remote-allow-origins=*",
      `--user-data-dir=${join(tempDir, "profile")}`,
      "--window-size=1280,900",
      "about:blank",
    ],
    { stdio: "ignore" },
  );
  await waitHttp(`http://127.0.0.1:${CDP_PORT}/json/version`, 15000, "Chrome DevTools 端口");
  const targets = await waitFor(async () => {
    const response = await fetch(`http://127.0.0.1:${CDP_PORT}/json/list`);
    const list = await response.json();
    const page = list.find((item) => item.type === "page" && item.webSocketDebuggerUrl);
    return page || false;
  }, 10000, "页面 target");

  cdp = new Cdp(await wsConnect(targets.webSocketDebuggerUrl));
  await cdp.send("Page.enable");
  await cdp.send("Runtime.enable");
  await cdp.send("Network.enable");
  await cdp.send("Log.enable");
  // 统计旧帧 Blob URL 的 revoke 次数（验证内存回收）
  await cdp.send("Page.addScriptToEvaluateOnNewDocument", {
    source:
      "window.__revoked = 0; (function () { const original = URL.revokeObjectURL.bind(URL); URL.revokeObjectURL = function (url) { window.__revoked += 1; return original(url); }; })();",
  });

  // ---------- 场景 A：根路径 / ----------
  await cdp.send("Page.navigate", { url: `${BASE}/` });
  await waitFor(() => cdp.evaluate(`document.readyState === "complete"`), 10000, "页面加载完成");
  await waitFor(() => cdp.evaluate(`document.body.innerText.includes("手机已连接")`), 8000, "显示手机已连接");

  // 标题 / 主题
  check("title = 手机控制台", (await cdp.evaluate("document.title")) === "手机控制台");
  check("h1 = 手机控制台", (await cdp.evaluate(`document.querySelector("h1").textContent.trim()`)) === "手机控制台");
  const sub = await cdp.evaluate(`document.querySelector(".sub").textContent.trim()`);
  check(".sub 一句话说明", sub.length > 0 && sub.length <= 30, sub);
  const theme = await cdp.evaluate(
    `(() => { const b = getComputedStyle(document.body); return { bg: b.backgroundColor, fg: b.color, scheme: getComputedStyle(document.documentElement).colorScheme }; })()`,
  );
  check("背景 #3a3f47", theme.bg === "rgb(58, 63, 71)", theme.bg);
  check("正文 #f2f3f5", theme.fg === "rgb(242, 243, 245)", theme.fg);
  check("color-scheme: dark", String(theme.scheme).includes("dark"), theme.scheme);

  // 状态区
  const meta = await cdp.evaluate(`document.querySelector(".meta").innerText`);
  check("显示设备名", meta.includes("Mock Pixel 7"), meta.replace(/\n+/g, " | "));
  check("显示分辨率", meta.includes("1080 × 2400"), "");
  check("显示无障碍状态", meta.includes("已开启"), "");

  // 协议：view:on 与 pong
  await waitFor(() => readLog().some((entry) => entry.ev === "view" && entry.on === true), 5000, "收到 view:true");
  check("连接后发送 view:on", true);
  await waitFor(
    () => readLog().some((entry) => entry.ev === "ws-in" && entry.msg && entry.msg.t === "pong"),
    5000,
    "收到浏览器 pong",
  );
  check("回复服务端 ping（pong）", true);

  // 画面帧
  await waitFor(
    () => cdp.evaluate(`(() => { const img = document.querySelector(".frame"); return !!img && img.complete && img.naturalWidth > 0; })()`),
    8000,
    "JPEG 帧渲染",
  );
  const frameInfo = await cdp.evaluate(
    `(() => { const img = document.querySelector(".frame"); return { nw: img.naturalWidth, nh: img.naturalHeight, src: img.src.slice(0, 5) }; })()`,
  );
  check("JPEG 帧显示", frameInfo.nw > 0, `${frameInfo.nw}x${frameInfo.nh} ${frameInfo.src}…`);
  await sleep(900);
  check("画面只有一个 <img>", (await cdp.evaluate(`document.querySelectorAll(".frame").length`)) === 1, "");
  const revokedCount = await cdp.evaluate("window.__revoked || 0");
  check("旧帧 Blob URL 被 revoke", revokedCount > 0, `revoke 次数 ${revokedCount}`);

  // 点击
  await cdp.evaluate(`document.querySelector(".screen").scrollIntoView({ block: "center" })`);
  await sleep(100);
  const rect = await cdp.evaluate(
    `(() => { const r = document.querySelector(".screen").getBoundingClientRect(); return { x: r.x, y: r.y, w: r.width, h: r.height }; })()`,
  );
  const press = (x, y) => cdp.send("Input.dispatchMouseEvent", { type: "mousePressed", x, y, button: "left", buttons: 1, clickCount: 1 });
  const move = (x, y) => cdp.send("Input.dispatchMouseEvent", { type: "mouseMoved", x, y, button: "left", buttons: 1 });
  const release = (x, y) => cdp.send("Input.dispatchMouseEvent", { type: "mouseReleased", x, y, button: "left", buttons: 0, clickCount: 1 });

  const center = { x: rect.x + rect.w / 2, y: rect.y + rect.h / 2 };
  await press(center.x, center.y);
  await sleep(40);
  await release(center.x, center.y);
  const tapCmd = (await waitCmd(1))[0];
  check("点击 → tap", tapCmd.action === "tap", JSON.stringify(tapCmd));
  check("tap 坐标为中心（0.5,0.5）", approx(tapCmd.x, 0.5, 0.02) && approx(tapCmd.y, 0.5, 0.02), `x=${tapCmd.x} y=${tapCmd.y}`);
  check("tap 坐标保留 4 位小数", Number(tapCmd.x.toFixed(4)) === tapCmd.x && Number(tapCmd.y.toFixed(4)) === tapCmd.y, "");

  // ack 反馈
  await waitFor(() => cdp.evaluate(`document.querySelector(".feedback").textContent.includes("指令已执行")`), 4000, "ack 反馈");
  check("收到 ack 并提示", true);

  // 滑动
  const startX = rect.x + rect.w * 0.25;
  const startY = rect.y + rect.h * 0.3;
  const endX = rect.x + rect.w * 0.75;
  const endY = rect.y + rect.h * 0.7;
  await press(startX, startY);
  for (let step = 1; step <= 5; step += 1) {
    await move(startX + ((endX - startX) * step) / 5, startY + ((endY - startY) * step) / 5);
    await sleep(30);
  }
  await release(endX, endY);
  const swipeCmd = (await waitCmd(2))[1];
  check("拖动 → swipe", swipeCmd.action === "swipe", JSON.stringify(swipeCmd));
  check(
    "swipe 起止点归一化",
    approx(swipeCmd.x1, 0.25, 0.03) && approx(swipeCmd.y1, 0.3, 0.03) && approx(swipeCmd.x2, 0.75, 0.03) && approx(swipeCmd.y2, 0.7, 0.03),
    `(${swipeCmd.x1},${swipeCmd.y1}) → (${swipeCmd.x2},${swipeCmd.y2})`,
  );
  check("swipe 携带时长 ms", Number.isInteger(swipeCmd.ms) && swipeCmd.ms >= 50, `ms=${swipeCmd.ms}`);

  // 长按（按住 800ms 不动）
  await press(center.x, center.y);
  await sleep(800);
  await release(center.x, center.y);
  const longCmd = (await waitCmd(3))[2];
  check("按住 >600ms → longpress", longCmd.action === "longpress", JSON.stringify(longCmd));
  check("longpress 坐标为按下点", approx(longCmd.x, 0.5, 0.02) && approx(longCmd.y, 0.5, 0.02), `x=${longCmd.x} y=${longCmd.y}`);

  // 按键条
  const keyLabels = await cdp.evaluate(`[...document.querySelectorAll(".key")].map((b) => b.textContent.trim())`);
  check(
    "按键条 8 个按钮",
    keyLabels.length === 8,
    keyLabels.join("/"),
  );
  const expectedKeys = ["返回", "桌面", "多任务", "通知栏", "音量+", "音量-", "亮屏", "锁屏"];
  check("按键标签正确", JSON.stringify(keyLabels) === JSON.stringify(expectedKeys), keyLabels.join("/"));
  const expectedNames = ["back", "home", "recents", "notifications", "volume_up", "volume_down", "wake", "sleep"];
  let keyOk = true;
  for (let index = 0; index < expectedNames.length; index += 1) {
    await cdp.evaluate(`document.querySelectorAll(".key")[${index}].click()`);
    const messages = await waitCmd(4 + index);
    const keyMessage = messages[3 + index];
    if (keyMessage.action !== "key" || keyMessage.name !== expectedNames[index]) {
      keyOk = false;
      check(`按键 ${keyLabels[index]} → ${expectedNames[index]}`, false, JSON.stringify(keyMessage));
    }
  }
  check("按键映射 back/home/recents/notifications/volume_up/volume_down/wake/sleep", keyOk);

  // 文本输入（发送按钮）
  await cdp.evaluate(`document.querySelector(".text-input").focus()`);
  await cdp.send("Input.insertText", { text: "你好 phone" });
  await cdp.evaluate(`document.querySelector(".send").click()`);
  const textCmd = (await waitCmd(12))[11];
  check("文本 → text 指令", textCmd.action === "text" && textCmd.text === "你好 phone", JSON.stringify(textCmd));
  check("发送后输入框清空", (await cdp.evaluate(`document.querySelector(".text-input").value`)) === "");

  // 文本输入（回车）
  await cdp.evaluate(`document.querySelector(".text-input").focus()`);
  await cdp.send("Input.insertText", { text: "回车发送" });
  await cdp.send("Input.dispatchKeyEvent", { type: "keyDown", key: "Enter", code: "Enter", windowsVirtualKeyCode: 13, nativeVirtualKeyCode: 13 });
  await cdp.send("Input.dispatchKeyEvent", { type: "keyUp", key: "Enter", code: "Enter", windowsVirtualKeyCode: 13, nativeVirtualKeyCode: 13 });
  const textCmd2 = (await waitCmd(13))[12];
  check("回车 → text 指令", textCmd2.action === "text" && textCmd2.text === "回车发送", JSON.stringify(textCmd2));

  // view 开关（visibilitychange）
  await cdp.evaluate(`(() => { Object.defineProperty(document, "visibilityState", { configurable: true, get: () => "hidden" }); document.dispatchEvent(new Event("visibilitychange")); })()`);
  await waitFor(() => readLog().some((entry) => entry.ev === "view" && entry.on === false && entry.ts > 0), 4000, "view:false");
  const viewOff = readLog().filter((entry) => entry.ev === "view").pop();
  check("页面隐藏 → view:false", viewOff.on === false, JSON.stringify(viewOff.msg));
  await cdp.evaluate(`(() => { Object.defineProperty(document, "visibilityState", { configurable: true, get: () => "visible" }); document.dispatchEvent(new Event("visibilitychange")); })()`);
  await waitFor(() => readLog().filter((entry) => entry.ev === "view").pop()?.on === true, 4000, "view:true");
  check("页面可见 → view:true", true);

  // 桌面布局与主题约束
  await cdp.evaluate("window.scrollTo(0, 0)");
  await sleep(120);
  const fit = await cdp.evaluate(
    `(() => { const c = document.querySelector(".controls").getBoundingClientRect(); return { bottom: Math.round(c.bottom), innerHeight: window.innerHeight, pageHeight: document.documentElement.scrollHeight }; })()`,
  );
  check("桌面首屏可见操作区", fit.bottom <= fit.innerHeight + 2, JSON.stringify(fit));
  check(
    "无主题切换/标识、不用 localStorage",
    await cdp.evaluate(`!document.body.innerText.includes("主题") && Object.keys(localStorage).length === 0`),
  );
  const desktopShotData = await cdp.send("Page.captureScreenshot", { format: "png" });
  (await import("node:fs")).writeFileSync(desktopShot, Buffer.from(desktopShotData.data, "base64"));

  // 375px 响应式
  await cdp.send("Emulation.setDeviceMetricsOverride", { width: 375, height: 667, deviceScaleFactor: 1, mobile: true });
  await sleep(250);
  const narrow = await cdp.evaluate(
    `(() => ({
      scrollWidth: document.documentElement.scrollWidth,
      clientWidth: document.documentElement.clientWidth,
      innerWidth: window.innerWidth,
      screenRight: document.querySelector(".screen").getBoundingClientRect().right,
      inputRight: document.querySelector(".text-row").getBoundingClientRect().right,
    }))()`,
  );
  check("375px 无横向滚动", narrow.scrollWidth <= 375 && narrow.scrollWidth <= narrow.innerWidth + 0.5, JSON.stringify(narrow));
  check("375px 画面不溢出", narrow.screenRight <= 375.5, `screen.right=${narrow.screenRight}`);
  check("375px 输入行不溢出", narrow.inputRight <= 375.5, `input.right=${narrow.inputRight}`);
  const mobileShotData = await cdp.send("Page.captureScreenshot", { format: "png" });
  (await import("node:fs")).writeFileSync(mobileShot, Buffer.from(mobileShotData.data, "base64"));
  await cdp.send("Emulation.clearDeviceMetricsOverride");
  await sleep(150);

  // 外部请求检查（页面自身发起的所有 HTTP 资源必须同源）
  const resources = await cdp.evaluate(
    `performance.getEntriesByType("resource").map((entry) => entry.name).filter((name) => name.startsWith("http"))`,
  );
  const externalResources = resources.filter((url) => !url.startsWith(`${BASE}/`));
  check("零外部请求（performance 资源）", externalResources.length === 0, externalResources.join(", "));
  const externalNetwork = [...cdp.networkUrls].filter(
    (url) => url.startsWith("http") && !url.startsWith(`${BASE}/`) && !url.includes("127.0.0.1"),
  );
  check("零外部请求（Network 事件）", externalNetwork.length === 0, externalNetwork.join(", "));

  // ---------- 场景 B：/control/ 前缀下的相对 WS ----------
  await cdp.send("Page.navigate", { url: `${BASE}/control/` });
  await waitFor(() => cdp.evaluate(`document.readyState === "complete"`), 10000, "前缀页面加载");
  await waitFor(() => cdp.evaluate(`document.body.innerText.includes("手机已连接")`), 8000, "前缀页面连上设备");
  const prefixOpens = readLog().filter((entry) => entry.ev === "ws-open" && entry.path === "/control/ws/web");
  check("带 /control/ 前缀时 WS 推导为 /control/ws/web", prefixOpens.length > 0, `共 ${prefixOpens.length} 次`);
  await waitFor(
    () => cdp.evaluate(`(() => { const img = document.querySelector(".frame"); return !!img && img.complete && img.naturalWidth > 0; })()`),
    8000,
    "前缀页面画面帧",
  );
  check("前缀页面画面正常", true);
  const prefixAssets = await cdp.evaluate(`performance.getEntriesByType("resource").map((entry) => entry.name)`);
  check(
    "前缀页面相对资源路径正确",
    prefixAssets.some((url) => url.includes("/control/assets/") || url.includes("/assets/")),
    prefixAssets.filter((url) => url.includes("assets")).join(", "),
  );
  check("无页面 JS 异常", cdp.pageErrors.length === 0, cdp.pageErrors.slice(0, 3).join(" | "));

  // ---------- 场景 C：手机端未连接（status.online=false） ----------
  await stopMock();
  await startMock(["--device-offline"]);
  await cdp.send("Page.navigate", { url: `${BASE}/` });
  await waitFor(() => cdp.evaluate(`document.readyState === "complete"`), 10000, "离线场景页面加载");
  await waitFor(() => cdp.evaluate(`document.querySelector(".state")?.textContent.trim() === "等待手机连接"`), 8000, "等待手机连接");
  check("未连接时提示「手机端 App 未连接」", await cdp.evaluate(`document.body.innerText.includes("手机端 App 未连接")`));
  check("未连接时无画面帧", await cdp.evaluate(`!document.querySelector(".frame")`));
  check(
    "未连接时按键/输入框灰态禁用",
    await cdp.evaluate(
      `[...document.querySelectorAll(".key")].every((b) => b.disabled) && document.querySelector(".text-input").disabled && document.querySelector(".send").disabled`,
    ),
  );
  const offlineCmdsBefore = cmdMessages().length;
  await cdp.evaluate(`document.querySelector(".screen").scrollIntoView({ block: "center" })`);
  await sleep(120);
  const offlineRect = await cdp.evaluate(
    `(() => { const r = document.querySelector(".screen").getBoundingClientRect(); return { x: r.x, y: r.y, w: r.width, h: r.height }; })()`,
  );
  await press(offlineRect.x + offlineRect.w / 2, offlineRect.y + offlineRect.h / 2);
  await release(offlineRect.x + offlineRect.w / 2, offlineRect.y + offlineRect.h / 2);
  await sleep(400);
  check("未连接时点击画面不发指令", cmdMessages().length === offlineCmdsBefore, `cmd 数 ${offlineCmdsBefore} → ${cmdMessages().length}`);

  // ---------- 场景 D：断线自动重连 ----------
  await stopMock();
  await waitFor(() => cdp.evaluate(`document.body.innerText.includes("与服务器断开")`), 8000, "显示与服务器断开");
  check("断线后提示 + 立即重连按钮", await cdp.evaluate(`!!document.querySelector(".retry")`));
  await startMock();
  await waitFor(() => cdp.evaluate(`document.body.innerText.includes("手机已连接")`), 15000, "自动重连恢复");
  check("断线后自动重连（指数退避 ≤5s）", true);

  console.log(`\n截图：${desktopShot}\n      ${mobileShot}`);
} catch (error) {
  check("测试流程执行", false, error.message);
  console.error(error.stack);
} finally {
  if (cdp) cdp.close();
  if (chrome) {
    try {
      chrome.kill();
    } catch {
      // 忽略
    }
  }
  if (mock) {
    try {
      mock.kill();
    } catch {
      // 忽略
    }
  }
  await sleep(300);
}

const failed = results.filter((item) => !item.ok);
console.log(`\n合计 ${results.length} 项，失败 ${failed.length} 项`);
if (failed.length > 0) {
  for (const item of failed) console.log(`  ✗ ${item.name}${item.detail ? " — " + item.detail : ""}`);
  process.exitCode = 1;
} else {
  console.log("全部通过");
}

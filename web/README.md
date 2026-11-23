# 手机控制台 · 电脑端网页（web/）

Vue 3 + Vite 单页控制台：通过 WebSocket 与 Python 服务端通信，实时查看手机画面并执行点击 / 滑动 / 长按 / 按键 / 文本输入。

## 目录

```
web/
├─ index.html                 入口（<title> 手机控制台）
├─ vite.config.js             base: './'（线上 /control/ 前缀必须相对路径）+ 本地 dev WS 代理
├─ src/
│  ├─ main.js                 挂载 Vue
│  ├─ style.css               灰色单主题设计变量与基础排版（模板原样）
│  ├─ App.vue                 页头 / 连接状态 / 设备信息 / 画面 / 操作区
│  ├─ components/
│  │  ├─ ScreenPanel.vue      <img> + Blob URL 画面、指针手势 → 归一化坐标
│  │  └─ ControlPanel.vue     8 个按键 + 文本输入框
│  ├─ composables/
│  │  └─ useRemote.js         WS 连接 / 重连 / 心跳 / view 开关 / 收发帧
│  └─ (lib/ 预留)
└─ dev/                       仅自测用，不参与构建
   ├─ mock_server.py          假服务端：静态 dist + WS ws/web（可 --device-offline）
   └─ e2e_check.mjs           无头 Chrome 端到端自测（CDP）
```

## 命令

```bash
cd web
npm install      # 首次
npm run dev      # 开发服务器 http://localhost:5173（WS 代理到 127.0.0.1:8090）
npm run build    # 产物 web/dist，资源为相对路径
npm run preview  # 预览构建产物（仅静态，需配合真实服务端使用）
```

## 本地预览 / 联调

1. **连真实服务端（推荐）**：`python server/server.py` 启动后会托管 `web/dist`，直接打开 <http://127.0.0.1:8090/>。手机端 App 连上后页面显示画面。
2. **开发模式**：`npm run dev`，页面由 Vite 提供，`ws/web` 由 `vite.config.js` 代理到 `127.0.0.1:8090`（需服务端已启动）。
3. **无服务端时自测**：`npm run build` 后运行
   ```bash
   python web/dev/mock_server.py --port 18093 --log C:\temp\mock.jsonl
   ```
   打开 <http://127.0.0.1:18093/> 即可看到假画面、测试所有操作；Ctrl+C 关掉，不长期占端口。

## 端到端自测

```bash
cd web
npm run build
node dev/e2e_check.mjs
```

自动启动假服务端 + 无头 Chrome（默认端口 18093 / 19222，可用 `MOCK_PORT`、`CDP_PORT` 覆盖），验证 46 项：标题与主题色、点击/滑动/长按归一化坐标、按键映射、文本帧、心跳 pong、`view` 开关、`/control/` 前缀下的 WS 推导、375px 无横向滚动、零外部请求、旧帧 Blob URL 回收等。

## 约定

- WS 地址由当前页面 URL 推导相对路径 `ws/web`（http→ws，https→wss），兼容线上 `https://wkeeli.cn/control/`（Caddy 会剥掉前缀）。
- 协议见仓库根目录 `PROTOCOL.md` 第 5 节；所有指令帧为 `{"t":"cmd","action":...}`。
- 颜色只使用 `--bg/--fg/--muted/--line/--hover`，无主题切换、无 UI 库 / 图标库 / CDN / 网络字体，零外部请求。

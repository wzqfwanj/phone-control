# 协议契约 v1（手机控制系统的唯一事实源）

本文件是三条链路的接口约定：**Android App → 服务器 → 浏览器**。任何实现改动都要先改本文件。

## 1. 拓扑

```
手机 App（Android）  ──WSS──┐
                            ├─> Python 服务（dev 主机 127.0.0.1:8090）──HTTP──> 静态 Vue 页面
电脑浏览器            ──WSS──┘                      ▲
                                                    │
                    阿里云 Caddy（wkeeli.cn，全站 Basic Auth）──frps──> dev 主机 frpc
```

- 本地服务端口：`8090`（只监听 `127.0.0.1`）
- frp 隧道：dev `127.0.0.1:8090` ← aliyun `127.0.0.1:18090`
- 浏览器入口：`https://wkeeli.cn/control/` （Caddy `handle_path /control/*`，带全站 Basic Auth）
- 手机入口：`wss://wkeeli.cn/agent/ws/device?key=<DEVICE_KEY>`（Caddy `handle /agent/*`，**不进 Basic Auth**，用设备配对码鉴权）
- 本地开发入口：`http://127.0.0.1:8090/` ，手机连 `ws://<电脑局域网IP>:8090/agent/ws/device?key=...`

服务端会把 `/agent` 前缀剥掉再路由，所以 `/agent/ws/device` 等价于 `/ws/device`。

## 2. 服务端 HTTP

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/healthz` | `{"ok": true}` |
| GET | `/` + 其他静态路径 | 从 `CONTROL_STATIC_DIR`（`web/dist`）读文件；`/` → `index.html`；未知路径回退 `index.html` |

前端所有资源必须用**相对路径**（线上前缀 `/control/` 可能被 Caddy 剥掉），Vite 配置 `base: './'`。

服务端环境变量：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `CONTROL_HOST` | `127.0.0.1` | 监听地址 |
| `CONTROL_PORT` | `8090` | 端口 |
| `CONTROL_DEVICE_KEY` | `''` | 设备配对码；空则拒绝所有设备连接 |
| `CONTROL_STATIC_DIR` | `web/dist` | 前端产物目录 |

## 3. WebSocket：共用心跳约定

- 客户端每 20s 发 `{"t":"ping"}`；服务端回 `{"t":"pong"}`。
- 服务端每 25s 发 `{"t":"ping"}`（纯文本帧，非 WS 控制帧），客户端必须回 `{"t":"pong"}`。
- 服务端 60s 内收不到任何帧即断开该连接（浏览器/手机各自独立计时）。
- 所有 JSON 文本帧都用 UTF-8，字段名短、小写下划线。二进制帧只用于 JPEG 画面。

## 4. 设备通道 `WS /ws/device?key=<KEY>`

握手：`key` 与 `CONTROL_DEVICE_KEY` 完全一致才接受，否则 HTTP 401（在 upgrade 前拒绝）。
同一时刻只保留**一台**设备：新设备连上时踢掉旧连接（发 `{"t":"bye","reason":"replaced"}` 后关闭）。

### 4.1 设备 → 服务端（文本）

| 消息 | 说明 |
| --- | --- |
| `{"t":"hello","name":"Pixel 7","w":1080,"h":2400,"accessibility":true,"version":1}` | 连接后立即发，之后可再发以更新状态 |
| `{"t":"frames","on":true}` | 可选：回报当前是否在推流 |
| `{"t":"ack","id":"c1"}` | 指令执行成功 |
| `{"t":"error","id":"c1","message":"accessibility not enabled"}` | 指令失败（可读原因） |
| `{"t":"pong"}` | 心跳应答 |

### 4.2 设备 → 服务端（二进制）

**一帧一个 JPEG**：二进制帧内容就是完整 JPEG 字节，无额外头。服务端缓存最后一帧（给新打开的浏览器秒显），并转发给所有浏览器连接。

### 4.3 服务端 → 设备（文本）

| 消息 | 说明 |
| --- | --- |
| `{"t":"viewers","n":1}` | 当前浏览器观看人数；`n==0` 时设备应暂停抓屏推流（省电），`n>0` 恢复 |
| `{"t":"cmd","id":"c1","action":"tap","x":0.5,"y":0.3}` | 执行指令，执行后必须回 `ack` 或 `error` |
| `{"t":"ping"}` | 心跳 |
| `{"t":"bye","reason":"replaced"}` | 即将断开 |

## 5. 浏览器通道 `WS /ws/web`（相对路径 `ws/web`）

### 5.1 浏览器 → 服务端（文本）

| 消息 | 说明 |
| --- | --- |
| `{"t":"view","on":true}` | 打开/关闭观看（默认关闭）；`on=false` 时服务端不下发画面 |
| `{"t":"cmd","action":"tap","x":0.5,"y":0.3}` | 指令；服务端补 `id` 后转发设备 |
| `{"t":"ping"}` | 心跳 |

指令集合（`action`）：

| action | 参数 | 语义 |
| --- | --- | --- |
| `tap` | `x,y` | 点击（归一化 0~1，相对屏幕宽高） |
| `swipe` | `x1,y1,x2,y2,ms` | 滑动，`ms` 默认 300 |
| `longpress` | `x,y,ms` | 长按，`ms` 默认 600 |
| `key` | `name` | `back` / `home` / `recents` / `notifications` / `enter` / `volume_up` / `volume_down` / `wake` / `sleep` |
| `text` | `text` | 向当前焦点输入框输入文本（≤ 500 字） |
| `quality` | `width,q,fps` | 调整画面清晰度：目标宽度像素（240~1080）、JPEG 质量（30~90）、帧率（1~15）；设备 `ack` 后生效 |

坐标归一化：`x = 像素x / 屏幕宽`、`y = 像素y / 屏幕高`，保留 4 位小数；服务端原样转发，由设备端换算。

### 5.2 服务端 → 浏览器（文本）

| 消息 | 说明 |
| --- | --- |
| `{"t":"status","online":true,"name":"Pixel 7","w":1080,"h":2400,"accessibility":true}` | 设备上线/状态变化；`online:false` 时其余字段可为 `null` |
| `{"t":"ack","id":"c1"}` | 设备已执行 |
| `{"t":"error","id":"c1","message":"..."}` | 设备执行失败 |
| `{"t":"pong"}` | 心跳应答 |

### 5.3 服务端 → 浏览器（二进制）

转发设备最新 JPEG 帧。浏览器按 `status.w/h` 做等比显示。

## 6. 配对与安全

- 设备配对码即 `CONTROL_DEVICE_KEY`（部署时随机生成，例如 `openssl rand -hex 12`）。
- App 内保存服务器地址 + 配对码，首次可手填；构建时可用 `android/private.properties` 预置（该文件不入库）。
- 浏览器侧不做登录，由 Caddy `wkeeli.cn` 全站 Basic Auth 收口（与 `/constraints/` 一致）。
- 服务端只监听回环地址，不直接暴露公网端口。

## 7. 范围边界（最小功能）

做：实时看屏幕、点击/滑动/长按、返回/桌面/多任务/通知栏、音量加减、亮屏/锁屏、输入文本、断线自动重连。
不做：文件传输、装 App、录屏保存、多设备同时控制、远程开机。

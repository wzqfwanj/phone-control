# 部署与发布（deploy）

手机远程控制系统的部署资产与操作手册。线上拓扑沿用现有 `constraints` / `context` 的
「dev 主机 systemd + frp 隧道 + aliyun Caddy 子路径 + 全站 Basic Auth」模式。

```
浏览器 ──HTTPS──> wkeeli.cn（aliyun 跳板机 Caddy v2.11.4，全站 Basic Auth）
                    ├─ /control/*  ──handle_path（剥前缀）──> 127.0.0.1:18090（frps 隧道）
                    └─ /agent/*    ──handle（不剥前缀、免登录）──> 127.0.0.1:18090
                                                                      │
手机 App ──WSS──> wss://wkeeli.cn/agent/ws/device?key=<KEY>           │
                                                                      ▼
dev 主机（180.76.180.249）：frpc 隧道 8090→18090，systemd 常驻
                            /opt/phone-control/.venv/bin/python server/server.py
                            只监听 127.0.0.1:8090
```

- 浏览器入口 `https://wkeeli.cn/control/`：走全站 Basic Auth（账号 `wkeeli`）。
- 手机入口 `wss://wkeeli.cn/agent/ws/device?key=<DEVICE_KEY>`：**不进 Basic Auth**
  （原生 App 的 WS 客户端没法带登录头），改由设备配对码鉴权，key 错误连接前即 401。
- 服务端只监听回环，公网流量只经 frps(18090) 与 Caddy(443) 进入。

## 交付物

| 文件 | 作用 |
| --- | --- |
| `phone-control.service` | systemd 单元：venv python 跑 `server/server.py`，读 `/opt/phone-control/.env`，Restart=always + 只读沙箱加固 |
| `env.example` | `.env` 模板（HOST/PORT/DEVICE_KEY/STATIC_DIR），真实 `.env` 不入库 |
| `push.ps1` | 一键同步 `server/` + `web/dist/` 到 dev 并重启服务、自检 healthz |
| `remote-install.sh` | 在 dev 主机执行的安装脚本（由 push.ps1 上传调用；幂等） |
| `frpc.snippet.toml` | frpc 隧道片段：`phone-control` tcp 8090→18090（不含 token） |
| `Caddyfile.snippet` | Caddy 两处改动的完整片段与插入位置说明 |
| `patch-online.py` | 线上配置改动工具（幂等；自动备份 + validate/verify + 失败回滚），可选但推荐 |
| `verify-online.py` | 外网验收脚本（401、页面、healthz、设备 WS 401/握手、浏览器 WS ping/pong） |
| `README.md` | 本文件 |

仓库根目录的 `README.md` 由项目主文档负责；本目录只放部署相关资产。

## 已知线上现状（2026-11-23 部署前核对）

- dev（180.76.180.249）：root + 免密 sudo，Python 3.12.3，venv 可用，`rsync`/`openssl`/`curl` 可用，联网正常。
  - `8090` 空闲（`8091` 被别的 python 占用，与本项目无关）。
  - `/opt/frp/frpc.toml` 原有 10 个 proxy：milvus、mysql、redis、redis-insight、pw、postgres、
    supersys、supercut、constraints、super-context；**无 `phone-control`，`remotePort=18090` 空闲**。
  - `frpc 0.61.1`（支持 `frpc verify -c`），服务名 `frpc.service`。
- aliyun（8.133.209.25）：root + 免密 sudo，Caddy v2.11.4（`caddy.service` active）。
  - `/etc/caddy/Caddyfile` 共 326 行；`wkeeli.cn { ... }` 块位于第 7–287 行；
  - `@not_local { ... }` 在第 11–16 行（`not remote_ip ...` + `not path /pc2mb/*`、`/record/*`、`/context/s/*`）；
  - `basic_auth @not_local` 在第 17–19 行；
  - 兜底 `reverse_proxy localhost:5000` 在第 274 行；
  - `/constraints/` 段在第 261–264 行，是「`handle_path` + `reverse_proxy 127.0.0.1:1808x`」的现成范式。
  - frps 已监听 18003/18070/18081/18082/18850/19530/13306/16379 等，**18090 空闲**。
- 站点 CSP 已有 `connect-src 'self'`（同源 wss 允许）、`img-src 'self' data: blob:`，
  部署本项目**不需要改 CSP**。
- 本机 `gh` 已登录 `wzqfwanj`（https，含 repo/workflow 权限）；GitHub 仓库 `wzqfwanj/phone-control` 尚未创建。

## 首次部署（已完成，供重装/迁移复现）

前置：本机（Windows）能 `ssh dev`、`ssh aliyun` 免密；`ssh`/`scp`/`tar` 可用；
前端已构建（`web/dist/index.html` 存在）；`server/` 已完成。

### 1. dev 主机：代码 + venv + systemd（一条命令）

```powershell
# 仓库根目录执行；首次会生成随机 DEVICE_KEY，之后始终保留 .env
powershell -ExecutionPolicy Bypass -File deploy/push.ps1 -ShowDeviceKey
```

脚本做的事：本地打 tar → scp 到 `dev:/tmp/phone-control-push` → 在 dev 上执行
`remote-install.sh`：

1. `/opt/phone-control/server/` ← `server/*.py` + `requirements.txt`（rsync --delete，旧文件会清掉）
2. `/opt/phone-control/web/dist/` ← `web/dist/`（原子替换 + 删除旧 hash 资源，权限 755/644）
3. `.env`：**已存在则原样保留**；不存在则用 `env.example` 生成，并写入 `openssl rand -hex 16` 的
   `CONTROL_DEVICE_KEY`（权限 600）
4. `.venv`：不存在则 `python3 -m venv`；`requirements.txt` 的 sha256 变化才 `pip install`（`websockets==17.4`）
5. 安装 `/etc/systemd/system/phone-control.service` → `daemon-reload` → `enable` → `restart`
6. 自检 `curl http://127.0.0.1:8090/healthz`（最多重试 10s），并确认只监听 `127.0.0.1:8090`
7. 清理远端 staging（失败时保留 staging 便于排查）

期望结尾：

```
==> healthz: {"ok":true}
==> listen: LISTEN 0 ... 127.0.0.1:8090 ... python
==> phone-control installed and running
```

常用命令：

```bash
ssh dev 'systemctl status phone-control --no-pager'
ssh dev 'journalctl -u phone-control -n 50 --no-pager'
ssh dev 'grep ^CONTROL_DEVICE_KEY= /opt/phone-control/.env'   # 配对码，只留在 dev，不要入库
```

### 2. dev 主机：frp 隧道 8090 → 18090

先备份（要求落在 `/opt/frp/frpc.toml.bak-phone-control-<时间戳>`）：

```powershell
ssh dev 'cp -a /opt/frp/frpc.toml /opt/frp/frpc.toml.bak-phone-control-$(date +%Y%m%d-%H%M%S)'
```

方式 A（推荐，幂等 + 自动校验/回滚）：

```powershell
scp deploy/patch-online.py dev:/tmp/patch-online.py
ssh dev 'python3 /tmp/patch-online.py frpc --dry-run'   # 只打印 diff 并校验临时副本
ssh dev 'python3 /tmp/patch-online.py frpc'            # 备份 → 追加 → frpc verify，失败自动还原
ssh dev 'systemctl restart frpc && systemctl is-active frpc'
```

方式 B（手工）：把 `deploy/frpc.snippet.toml` 的 `[[proxies]]` 5 行**追加**到
`/opt/frp/frpc.toml` 末尾，然后：

```bash
cd /opt/frp && ./frpc verify -c /opt/frp/frpc.toml && systemctl restart frpc
```

验证隧道（在 aliyun 上）：

```powershell
ssh aliyun 'curl -s http://127.0.0.1:18090/healthz'   # {"ok":true}
ssh aliyun 'ss -ltnp | grep 18090'                     # LISTEN ... frps
```

> `restart frpc` 会让所有隧道瞬断一下（毫秒级），现有 constraints/context 等会自动重连。

### 3. aliyun：Caddy 反代（改 `@not_local` + 插入两个 handle）

先备份（要求落在 `/etc/caddy/Caddyfile.bak-phone-control-<时间戳>`）：

```powershell
ssh aliyun 'cp -a /etc/caddy/Caddyfile /etc/caddy/Caddyfile.bak-phone-control-$(date +%Y%m%d-%H%M%S)'
```

方式 A（推荐，幂等 + 自动 validate/回滚）：

```powershell
scp deploy/patch-online.py aliyun:/tmp/patch-online.py
ssh aliyun 'python3 /tmp/patch-online.py caddy --dry-run'  # 打印 diff + caddy validate 临时副本
ssh aliyun 'python3 /tmp/patch-online.py caddy'            # 备份 → 改 → caddy validate，失败自动还原
ssh aliyun 'caddy validate --config /etc/caddy/Caddyfile && systemctl reload caddy'
```

方式 B（手工）：按 `deploy/Caddyfile.snippet` 改两处——

1. 在 `@not_local { ... }` 里 `not path /context/s/*` 之后加一行 `not path /agent/*`
   （**改已有块，不是粘新块**）；原因：手机 App 无法带 Basic Auth，必须让 `/agent/*`
   绕过全站登录，改由配对码鉴权。`/control/*` 不要排除，浏览器页面继续走登录。
2. 在兜底语句 `reverse_proxy localhost:5000` **之前**，插入片段里的
   `redir /control /control/ 308` + `handle_path /control/* {...}` + `handle /agent/* {...}`
   两个 handler（都 `reverse_proxy 127.0.0.1:18090`，必须 `flush_interval -1` 关缓冲）。

```bash
sudo caddy validate --config /etc/caddy/Caddyfile && sudo systemctl reload caddy
```

验证（在 aliyun 本机；`--resolve` 让 SNI/Host 命中 wkeeli.cn，回环请求本来就不需要登录）：

```bash
curl -sk --resolve wkeeli.cn:443:127.0.0.1 -o /dev/null -w '%{http_code}\n' https://wkeeli.cn/control/          # 200
curl -sk --resolve wkeeli.cn:443:127.0.0.1 https://wkeeli.cn/control/healthz                                    # {"ok":true}
curl -sk --resolve wkeeli.cn:443:127.0.0.1 -o /dev/null -w '%{http_code}\n' https://wkeeli.cn/control            # 308
# 设备通道错 key：401 且响应头无 WWW-Authenticate（说明命中应用，而非 Caddy 登录页）
curl -sk --resolve wkeeli.cn:443:127.0.0.1 -D - -o /dev/null 'https://wkeeli.cn/agent/ws/device?key=wrong'
# 浏览器 WS 反转：101 Switching Protocols（HTTP/1.1 才支持 Upgrade）
curl -s -i --http1.1 --max-time 3 -H 'Connection: Upgrade' -H 'Upgrade: websocket' \
     -H 'Sec-WebSocket-Version: 13' -H 'Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==' \
     https://wkeeli.cn/control/ws/web
```

### 4. 外网验收

```powershell
# 推荐用环境变量传密码，避免进命令历史；DEVICE_KEY 从 dev 的 .env 取
$env:WKEELI_USER = 'wkeeli'
$env:WKEELI_PASSWORD = '<全站密码>'
$env:CONTROL_DEVICE_KEY = (ssh dev 'grep ^CONTROL_DEVICE_KEY= /opt/phone-control/.env').Split('=')[1]
python deploy/verify-online.py
```

6 项全 PASS 才算通过：

| # | 检查 | 期望 |
| --- | --- | --- |
| 1 | `GET https://wkeeli.cn/control/` 不带凭据 | 401 + `WWW-Authenticate: Basic` |
| 2 | 带全站账号 | 200，页面含「手机控制台」，资源是相对路径 `./assets/` |
| 3 | 带账号 `GET /control/healthz` | 200 `{"ok": true}` |
| 4 | `wss://…/agent/ws/device?key=错` | 握手 HTTP 401 |
| 5 | `wss://…/agent/ws/device?key=<DEVICE_KEY>` | 握手成功（会踢掉当前已连手机，协议 `replaced`） |
| 6 | `wss://…/control/ws/web`（Basic Auth） | 握手成功，发 `{"t":"ping"}` 收到 `{"t":"pong"}` |

浏览器人工验收：打开 <https://wkeeli.cn/control/>，输入全站账号 → 看到「手机控制台」；
手机 App 填 `wss://wkeeli.cn/agent/ws/device` + 配对码连接后，页面应显示设备在线与实时画面。

> 手机正在使用时不要跑第 4/5 项（会踢掉真机连接）；可加 `--skip-device` 只验收页面链路。
> 仅排查证书问题时才用 `--insecure`，正常验收必须验证 TLS。

### 5. GitHub 发布（等 APK 定稿后执行）

```powershell
# 仓库根目录；仓库尚未创建
gh repo create wzqfwanj/phone-control --public --source=. --remote=origin --push

# 后续推送
git push -u origin main
```

APK 由 android 任务产出，`*.apk` 在 `.gitignore` 里，**只作为 Release 资产上传**：

```powershell
gh release create v0.1.0 android/build/phone-control.apk `
  --title "手机控制系统 v0.1.0" `
  --notes "首个公开版本：浏览器实时看屏/点击/滑动/长按/按键/文本输入；Android 端投屏 + 无障碍远控。安装后在 App 内填写服务器地址与配对码。"
gh release view v0.1.0 --web
```

通用版 APK 不内置配对码/服务器地址（`android/private.properties` 不入库），用户安装后手填。
发布前检查仓库里没有敏感信息：

```powershell
git ls-files | Select-String -Pattern '\.env|private\.properties|\.apk$|\.jks|keystore'   # 应为空
git grep -n 'CONTROL_DEVICE_KEY=' -- ':!deploy/env.example'                               # 只应有空值模板
```

## 日常更新（改了 server/ 或 web/）

```powershell
cd web; npm run build; cd ..                            # 只有前端改动才需要，产物进 web/dist
powershell -ExecutionPolicy Bypass -File deploy/push.ps1
```

`push.ps1` 幂等：`.env`（DEVICE_KEY）永远保留；`requirements.txt` 没变就不重复 pip；
systemd 单元每次覆盖安装并 restart。改 frp/Caddy 配置才需要重跑第 2/3 步。

## 回滚

```powershell
# 应用
ssh dev 'systemctl disable --now phone-control'         # 或恢复旧 web/dist + server/ 后 push

# frp（时间戳按实际备份名替换）
ssh dev 'cp -a /opt/frp/frpc.toml.bak-phone-control-<TS> /opt/frp/frpc.toml && systemctl restart frpc'

# Caddy（时间戳按实际备份名替换）
ssh aliyun 'cp -a /etc/caddy/Caddyfile.bak-phone-control-<TS> /etc/caddy/Caddyfile'
ssh aliyun 'caddy validate --config /etc/caddy/Caddyfile && systemctl reload caddy'
```

回滚 Caddy 后 `/control/`、`/agent/` 立即 404，不影响同机其它站点。

## 排障

| 现象 | 排查 |
| --- | --- |
| 外网 `/control/` 502 | `ssh dev 'systemctl status phone-control'`、`journalctl -u phone-control -n 100`、`ss -ltnp \| grep 8090`；再查 `ssh aliyun 'systemctl status frps'` |
| 外网 `/control/` 404 | Caddy 是否 reload 成功、`handle_path /control/*` 是否在兜底 `reverse_proxy localhost:5000` 之前 |
| `/agent/` 返回 401 且带 `WWW-Authenticate: Basic` | `@not_local` 里漏了 `not path /agent/*`（命中 Caddy 登录而不是应用） |
| `/agent/` 返回 401 且 body 是 `unauthorized: invalid device key` | 应用的配对码不匹配：核对 App 里填的 key 与 dev `.env` 的 `CONTROL_DEVICE_KEY` |
| `/agent/` 返回 426 / 400 | 正常 HTTP 请求而不是 WS 升级；用 `--http1.1` + Upgrade 头或浏览器/App 验证 |
| 页面白屏、资源 404 | 前端没按 `base:'./'` 构建；确认 `web/dist/index.html` 里是 `./assets/...` |
| 手机连不上、页面「未连接设备」 | `ssh dev 'journalctl -u phone-control -n 50'` 看是否 `connection rejected`；确认手机能访问 `wss://wkeeli.cn/agent/...` |
| healthz 通过但外网不通 | frp/Caddy 隧道：`ssh aliyun 'curl -s http://127.0.0.1:18090/healthz'` |
| `pip install` 失败 | dev 主机需能访问 PyPI（本项目只有 `websockets==17.4`） |
| push.ps1 报 `web/dist/index.html not found` | 先 `cd web; npm run build` |

## 安全注意

- 真实 `.env`（含 DEVICE_KEY）只存在 dev `/opt/phone-control/.env`，权限 600，**永不入库**；
  README/聊天/Release notes 里也不要写明文 key。
- 仓库不得出现：全站 Basic Auth 的 bcrypt 哈希、`.env`、`private.properties`、keystore、APK 私钥。
  `Caddyfile.snippet` 只写占位符，绝不复制站点里的 `basic_auth` 行。
- 服务端只监听 `127.0.0.1`（systemd 单元 + `.env` 双保险，remote-install.sh 会检查监听地址）。
- 换配对码：改 dev `.env` 的 `CONTROL_DEVICE_KEY` → `systemctl restart phone-control` → App 重填；
  旧 key 立即失效，已连接设备会被断开。
- Caddy 改动必须先备份、`caddy validate` 通过才 `reload`；frpc 改动先备份、`frpc verify` 通过才 `restart`。

## 本轮实测记录（2026-11-23）

| 项目 | 结果 |
| --- | --- |
| dev 目录 | `/opt/phone-control/{server,web/dist,.venv,.env}`，`.env` 600 root:root |
| systemd | `phone-control` enabled + active；ExecStart=`/opt/phone-control/.venv/bin/python server/server.py`；监听 `127.0.0.1:8090` |
| venv | `websockets==17.4`（首次部署 pip 安装成功） |
| dev healthz | `{"ok":true}` |
| frpc | 备份 `/opt/frp/frpc.toml.bak-phone-control-20261123-132900`；追加 `phone-control`（第 11 个 proxy）；`frpc verify` OK；`frpc` active；aliyun `curl 127.0.0.1:18090/healthz` = `{"ok":true}` |
| 其它隧道 | 重启 frpc 后 18081(`constraints`)/18082(`context`) 仍 200，18070/18003 仍可连通 |
| Caddyfile | 备份 `/etc/caddy/Caddyfile.bak-phone-control-20261123-132907`；第 16 行 `not path /agent/*`；第 279 行 `handle_path /control/*`；第 285 行 `handle /agent/*`；兜底语句移到第 291 行；`caddy validate` = Valid configuration；`caddy` reload 后 active |
| 外网（本机公网） | `/control/` 无凭据 → 401 + `WWW-Authenticate: Basic realm="restricted"`；`/control/healthz` 无凭据 → 401；`/agent/ws/device?key=wrong`（HTTP 与 WSS）→ 401；`wss://…/agent/ws/device?key=<正确>` → 握手成功并收到 `{"t":"viewers","n":0}` |
| 公网入口（allowlist IP 验证） | 200 页面（title「手机控制台」、资源 `./assets/` 相对路径）；`/control/healthz`=`{"ok":true}`；`/control/ws/web` 经 HTTP/1.1 Upgrade = `101 Switching Protocols`（server: phone-control） |
| 待补 | 带全站账号的外网 200 与浏览器 WS ping/pong：需 `WKEELI_PASSWORD` 后跑 `deploy/verify-online.py` 全量确认 |
| 待办 | GitHub 公开仓库 + Release 上传 APK（等 android 任务定稿，Lead 通知后执行） |

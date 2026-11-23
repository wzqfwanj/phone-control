# Android App：手机控制（投屏 + 无障碍远控）

按 `PROTOCOL.md` 第 4 节实现设备端。纯 Java、零第三方依赖、**不用 Gradle**，用手工构建链
（`aapt2` → `javac` → `d8` → 打包 → `zipalign` → `apksigner`）一条命令出 APK。

- 包名：`cn.wkeeli.phonecontrol`
- `minSdk 26`（Android 8.0），`targetSdk 34`（Android 14）
- 产物：`android/build/phone-control.apk`

## 1. 构建

前置（已在本机就绪，脚本自动探测）：

| 工具 | 位置 |
| --- | --- |
| JDK 17 | 环境变量 `JAVA_HOME` 或 `C:\dev-tools\jdk-17*` |
| Android SDK | `ANDROID_HOME` / `ANDROID_SDK_ROOT` 或 `C:\Android\Sdk` |
| 需要的 SDK 包 | `platforms;android-34`、`build-tools;34.0.0`、`platform-tools` |

```powershell
powershell -ExecutionPolicy Bypass -File android/build.ps1            # 构建 + 签名
powershell -ExecutionPolicy Bypass -File android/build.ps1 -Verify    # 构建 + 校验（badging/证书/APK 内容）
powershell -ExecutionPolicy Bypass -File android/build.ps1 -SkipSign  # 只构建不签名（不产出可安装包）
```

脚本做的事：生成 `BuildConfig.java` → `aapt2 compile` → `aapt2 link` → `javac`（`-source/-target 8`，
`-bootclasspath android.jar`）→ `jar` 打包 class → `d8 --min-api 26` 出 `classes.dex` → 用 .NET `ZipFile`
把 dex 塞进 aapt2 产出的 apk → `zipalign -p -f 4` → 首次自动 `keytool` 生成 `android/debug.keystore`
（`*.keystore` 已在 `.gitignore`）→ `apksigner sign`。

> 注意：`aapt2.exe`（原生程序）无法打开含非 ASCII 字符的**绝对**路径，脚本会先切到 `android/`
> 再用相对路径调用它；把工程放到中文路径下也能正常构建。

### 构建期配置（`android/private.properties`，不入库）

复制 `android/private.properties.example` 为 `android/private.properties` 后修改：

```properties
server.url=wss://wkeeli.cn/agent/ws/device   # 默认值；局域网调试可写 ws://192.168.1.100:8090/agent/ws/device
device.key=                                  # 配对码；留空则 App 首次运行提示手填
keystore.path=                               # 可选，留空用 android/debug.keystore
keystore.pass=android
key.alias=androiddebugkey
key.pass=android
```

这两个值会编译进 `BuildConfig.SERVER_URL` / `BuildConfig.DEVICE_KEY`，作为 App 输入框的默认值；
用户在 App 里改过后会存进 `SharedPreferences`，下次优先用用户值。

### 校验命令（可单独执行）

```powershell
C:\Android\Sdk\build-tools\34.0.0\aapt2.exe dump badging android\build\phone-control.apk
C:\Android\Sdk\build-tools\34.0.0\apksigner.bat verify --print-certs android\build\phone-control.apk
C:\Android\Sdk\build-tools\34.0.0\zipalign.exe -c -v -p 4 android\build\phone-control.apk
```

## 2. 安装到手机

```powershell
# USB 连接（手机开「开发者选项 → USB 调试」）或模拟器
C:\Android\Sdk\platform-tools\adb.exe install -r android\build\phone-control.apk
```

也可以把 apk 传到手机上用文件管理器点击安装（需允许「安装未知来源应用」）。

## 3. 手机端使用步骤（重要）

1. **装 App**：按上一步安装。
2. **开无障碍**：系统设置 → 无障碍（部分机型在「辅助功能」）→ 已下载的服务 / 已安装的服务 →
   「手机控制（远程操作）」→ 打开。App 主界面会显示「无障碍：已开启」。
   - 这一步必须做，否则点击/滑动/长按/按键/文本输入都不可用（会收到 `accessibility not enabled` 错误）。
   - 小米/华为等机型可能还要在「无障碍 → 已下载的服务」里允许该服务保持运行，或关闭电池优化。
3. **填服务器地址 + 配对码**：主界面两个输入框。
   - 线上：`wss://wkeeli.cn/agent/ws/device` + 服务端 `CONTROL_DEVICE_KEY` 的值。
   - 局域网调试：`ws://<电脑局域网IP>:8090/agent/ws/device` + 配对码。
   - 若没填配对码，连接会被服务端 401 拒绝。
4. **点「开始投屏」**：先在系统弹窗里允许通知（Android 13+），再在「Start recording or casting
   with 手机控制?」弹窗里点 **START NOW**（允许屏幕录制）。这是系统级授权，每次重启服务都要重新点。
5. 启动成功后会有常驻通知「手机控制」，可点通知里的「停止」结束投屏。
6. **电脑端**：浏览器打开控制台（线上 `https://wkeeli.cn/control/`，本地 `http://127.0.0.1:8090/`），
   设备在线后即可看到画面并操作。

### 需要的权限

| 权限 | 为什么 |
| --- | --- |
| `INTERNET` | 连服务端 WebSocket |
| `FOREGROUND_SERVICE` + `FOREGROUND_SERVICE_MEDIA_PROJECTION` | 投屏必须常驻前台服务（Android 14 要求先 `startForeground(type=mediaProjection)` 再 `getMediaProjection`） |
| `POST_NOTIFICATIONS` | Android 13+ 前台服务通知 |
| `WAKE_LOCK` | `wake`（亮屏）指令 |
| 无障碍服务（BIND_ACCESSIBILITY_SERVICE） | `dispatchGesture` 点击/滑动/长按、全局动作、`ACTION_SET_TEXT` 文本输入 |

> `usesCleartextTraffic="true"` 是为了局域网 `ws://` 调试；**线上走 `wss://`（强制 TLS，
> 且客户端做了证书链 + 主机名校验）**，不要把生产地址配成明文 `ws://`。

## 4. 协议对接（PROTOCOL.md §4）

| 方向 | 消息 | 实现 |
| --- | --- | --- |
| 设备→服务端 | `hello` | 连接（及重连）后立即发；`name`=`Build.MODEL`、`w`/`h`=当前屏幕像素、`accessibility`=无障碍是否已连接、`version=1`；无障碍状态变化、屏幕旋转后重发 |
| 设备→服务端 | `frames{on}` | 推流状态变化时发（`viewers>0` 且连接在线为 `on:true`） |
| 设备→服务端 | `ack` / `error` | 每条指令都回，带 `id`；手势在 `GestureResultCallback` 完成后回，失败回 error（如无障碍未开） |
| 设备→服务端 | 二进制 | 一帧一个 JPEG（最大边 540、质量 60），无额外头 |
| 设备→服务端 | `pong` | 收到服务端 `{"t":"ping"}` 后回 |
| 服务端→设备 | `viewers{n}` | `n>0` 才开始抓屏推流，`n==0` 暂停（省电） |
| 服务端→设备 | `cmd` | 见下表 |
| 服务端→设备 | `bye` | `reason=replaced` 时主动断开且不自动重连（避免两台设备互相踢） |

指令：`tap(x,y)`、`swipe(x1,y1,x2,y2,ms)`、`longpress(x,y,ms)`、`key(name)`、`text(text)`。
坐标按归一化 0~1 乘当前屏幕宽高换算；`key` 支持 `back/home/recents/notifications/enter/
volume_up/volume_down/wake/sleep`，其中 `sleep`（锁屏）需要 Android 9+，`enter` 需要 Android 11+，
其余均为 `AccessibilityService` 全局动作或 `dispatchGesture`。`text` 走当前焦点输入框的
`ACTION_SET_TEXT`，没有焦点输入框时回 error。

WebSocket 客户端（`WsClient.java`）是自研最小实现：RFC6455 握手（含 `Sec-WebSocket-Accept` 校验）、
客户端掩码帧、text/binary/ping/pong/close、分片重组、20s 应用层心跳、60s 无帧判死、
断线重连指数退避 1s→10s（带随机抖动）；`wss` 走 `SSLSocket` 并显式开启
`setEndpointIdentificationAlgorithm("HTTPS")` 做证书链 + 主机名校验。

## 5. 投屏实现要点

- `MediaProjection` + `VirtualDisplay(VIRTUAL_DISPLAY_FLAG_AUTO_MIRROR)` + `ImageReader(RGBA_8888)`。
- 抓到的帧按 `rowStride` 处理 padding，再缩放到**最大边 540**，JPEG 质量 60，约 **5fps 上限**
  （200ms 节流）。注意 `MediaProjection` 是**画面变化驱动**的：屏幕静止时不会产生新帧，
  所以实际帧率取决于屏幕内容变化频率，静止画面下不重复发送同一帧（省流量）。
- `viewers==0` 时不抓帧、不发帧。
- 屏幕旋转/分辨率变化：`DisplayManager.DisplayListener` 检测到后重建 `VirtualDisplay` 并重发 `hello`。
- 前台服务开始时顺序严格为 `startForeground(..., FOREGROUND_SERVICE_TYPE_MEDIA_PROJECTION)`
  → `getMediaProjection(...)`（Android 14 强制要求，否则会抛 SecurityException）。

## 6. 目录结构

```
android/
├─ AndroidManifest.xml                  # 权限 / Activity / 前台服务 / 无障碍服务声明
├─ build.ps1                            # 一键手工构建（无 Gradle）
├─ smoke-test.ps1                       # WsClient 冒烟自测（纯 JDK，不需要设备）
├─ private.properties.example           # 构建期配置模板（private.properties 不入库）
├─ res/
│  ├─ values/strings.xml
│  └─ xml/accessibility_service_config.xml
├─ src/cn/wkeeli/phonecontrol/
│  ├─ MainActivity.java                 # 地址/配对码、授权、状态展示
│  ├─ ScreenService.java                # 前台服务：投屏、WS 收发、指令分发、ack/error
│  ├─ RemoteAccessibilityService.java   # 无障碍：手势/全局动作/文本
│  └─ WsClient.java                     # 自研最小 RFC6455 WebSocket 客户端（可脱离 Android 单独测）
└─ tools/                               # 仅测试用，不进 APK
   ├─ cn/wkeeli/phonecontrol/WsSmokeTest.java
   ├─ ws_echo_server.py                 # 最小 WS 回声服务端（验证 WsClient 用）
   └─ web_probe.py                      # 假浏览器：验帧转发 + 指令 ack
```

## 7. 自测

```powershell
# 1) WebSocket 客户端自测（自动起 tools/ws_echo_server.py，纯 JDK）
powershell -ExecutionPolicy Bypass -File android/smoke-test.ps1

# 2) 也可以直接测真服务端
powershell -ExecutionPolicy Bypass -File android/smoke-test.ps1 -Url "ws://127.0.0.1:8090/agent/ws/device?key=devkey"

# 3) 模拟器/真机联调：起服务端 → 装 APK → 开无障碍 → 开始投屏，然后用假浏览器验整链路
$env:CONTROL_DEVICE_KEY='devkey'; python server/server.py
python android/tools/web_probe.py 8090 127.0.0.1
```

`web_probe.py` 会连 `/ws/web`、发 `view:on`、先发 `key:notifications` 制造画面变化（MediaProjection
只在画面变化时出帧），校验：`status.online/accessibility`、收到的帧是合法 JPEG、`key` 与 `tap`
都能收到 `ack`。

## 8. 已验证情况与已知限制

已在本机验证（模拟器 Android 11 / API 30，480×854）：

- `build.ps1` 退出码 0，产出 `android/build/phone-control.apk`；`aapt2 dump badging` 包名
  `cn.wkeeli.phonecontrol`、`sdkVersion 26`、`targetSdkVersion 34`、5 个权限全部正确；
  `apksigner verify --print-certs` 通过；`zipalign` 校验通过。
- APK 安装成功；无障碍服务被系统绑定（`capabilities` 含 `CAN_PERFORM_GESTURES`）；
  MediaProjection 授权弹窗点「START NOW」后前台服务正常起来。
- 连接真实 `server/server.py`：服务端日志收到 `device hello: {'w': 480, 'h': 854, 'accessibility': True}`；
  假浏览器 `view:on` 后收到合法 JPEG 帧（304×540，一帧约 14KB），画面确认是模拟器屏幕。
- 指令全链路：`key:notifications / volume_up / volume_down / back / home`、`swipe`、`longpress`、
  `tap` 均收到 `ack`（7/7）；`text` 在无焦点输入框时正确回 `error`（"没有找到焦点输入框"）。
- `WsClient` 冒烟自测：握手 / text / binary / 服务端消息 / ping-pong 全通过。

未验证/风险：

- 只在模拟器 API 30 验证过；**没有真机、没有 Android 13/14 真机**跑过。Android 14 的
  `startForeground(type=mediaProjection)` 顺序按官方文档实现，但未在 API 34 真机实测。
- 线上 `wss://wkeeli.cn` 未连真域名验证（TLS 主机名校验逻辑已加，证书异常时状态栏会显示
  「证书/主机名校验失败（可能证书过期或被中间人劫持）」）。
- 不同 ROM 的无障碍授权路径不同（尤其 MIUI/EMUI），可能需要用户手动允许自启动/后台运行。
- `sleep`（锁屏）依赖 Android 9+ 的 `GLOBAL_ACTION_LOCK_SCREEN`；`enter` 依赖 Android 11+；
  旧系统会回 error 说明原因。
- 帧率受画面变化驱动，静止画面不重复发帧；如果浏览器端要求"静止也持续刷新"，需要在服务端或
  设备端加定时重发最后一帧（当前按省流量实现，未做）。
- 编码参数（540 长边 / q60 / 5fps）在模拟器上观感正常，真机大屏可再调。

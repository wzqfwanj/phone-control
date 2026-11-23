// 浏览器 ↔ 服务端 WebSocket 通道（PROTOCOL.md 第 5 节）
// 只负责连接、重连、心跳、视口开关、收发消息与画面帧，不掺 UI。
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from "vue";

const PING_INTERVAL_MS = 20000;
const RECONNECT_MIN_MS = 500;
const RECONNECT_MAX_MS = 5000;

const ACTION_LABELS = {
  tap: "点击",
  swipe: "滑动",
  longpress: "长按",
  key: "按键",
  text: "文本输入",
};

/**
 * 由当前页面 URL 推导 `ws/web`：
 * - 相对路径，兼容线上 /control/ 前缀（Caddy handle_path 会剥掉前缀）
 * - http → ws，https → wss
 */
export function deriveWsUrl(loc = window.location) {
  const page = new URL(loc.href);
  page.hash = "";
  page.search = "";
  if (!page.pathname.endsWith("/")) page.pathname += "/";
  const url = new URL("ws/web", page.href);
  url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
  return url.href;
}

export function useRemote() {
  /** connecting | open | closed */
  const socketState = ref("connecting");
  /** { online, name, w, h, accessibility } */
  const device = ref({ online: false, name: null, w: 0, h: 0, accessibility: null });
  /** 当前画面帧的 Blob URL */
  const frameUrl = ref("");
  /** 最近一条指令反馈 */
  const feedback = ref("");

  const connected = computed(() => socketState.value === "open" && device.value.online);

  let socket = null;
  let pingTimer = null;
  let reconnectTimer = null;
  let reconnectAttempt = 0;
  let frameObjectUrl = "";
  let disposed = false;

  // ---------- 收发 ----------

  function send(payload) {
    if (!socket || socket.readyState !== WebSocket.OPEN) return false;
    socket.send(JSON.stringify(payload));
    return true;
  }

  function sendView(on) {
    send({ t: "view", on: !!on });
  }

  /** 发送一条控制指令；未连接设备时返回 false */
  function sendCmd(action, params = {}) {
    if (!connected.value) return false;
    const ok = send({ t: "cmd", action, ...params });
    if (ok) feedback.value = `已发送：${ACTION_LABELS[action] || action}`;
    return ok;
  }

  // ---------- 接收 ----------

  function applyStatus(msg) {
    const prev = device.value;
    device.value = {
      online: !!msg.online,
      name: typeof msg.name === "string" && msg.name ? msg.name : prev.name,
      w: Number(msg.w) > 0 ? Number(msg.w) : prev.w,
      h: Number(msg.h) > 0 ? Number(msg.h) : prev.h,
      accessibility:
        typeof msg.accessibility === "boolean" ? msg.accessibility : prev.accessibility,
    };
  }

  function handleText(raw) {
    let msg;
    try {
      msg = JSON.parse(raw);
    } catch {
      return;
    }
    if (!msg || typeof msg !== "object") return;
    switch (msg.t) {
      case "status":
        applyStatus(msg);
        break;
      case "ack":
        feedback.value = "指令已执行";
        break;
      case "error":
        feedback.value = `执行失败：${msg.message || "未知原因"}`;
        break;
      case "ping":
        // 服务端文本心跳，必须回 pong（PROTOCOL.md 第 3 节）
        send({ t: "pong" });
        break;
      case "pong":
      default:
        break;
    }
  }

  function handleFrame(blob) {
    const next = URL.createObjectURL(blob);
    const previous = frameObjectUrl;
    frameObjectUrl = next;
    frameUrl.value = next;
    // 新帧进入 DOM 后再释放旧 URL，避免画面闪断
    if (previous) nextTick(() => URL.revokeObjectURL(previous));
  }

  // ---------- 心跳 ----------

  function startPing() {
    stopPing();
    pingTimer = setInterval(() => {
      send({ t: "ping" });
    }, PING_INTERVAL_MS);
  }

  function stopPing() {
    if (pingTimer !== null) {
      clearInterval(pingTimer);
      pingTimer = null;
    }
  }

  // ---------- 连接 / 重连 ----------

  function clearReconnect() {
    if (reconnectTimer !== null) {
      clearTimeout(reconnectTimer);
      reconnectTimer = null;
    }
  }

  function scheduleReconnect() {
    if (disposed || reconnectTimer !== null) return;
    reconnectAttempt += 1;
    const delay = Math.min(
      RECONNECT_MIN_MS * 2 ** (reconnectAttempt - 1),
      RECONNECT_MAX_MS,
    );
    reconnectTimer = setTimeout(() => {
      reconnectTimer = null;
      connect();
    }, delay);
  }

  function connect() {
    if (disposed) return;
    clearReconnect();
    socketState.value = "connecting";

    let ws;
    try {
      ws = new WebSocket(deriveWsUrl());
    } catch {
      socketState.value = "closed";
      scheduleReconnect();
      return;
    }
    ws.binaryType = "blob";
    socket = ws;

    ws.onopen = () => {
      if (socket !== ws) return;
      reconnectAttempt = 0;
      socketState.value = "open";
      feedback.value = "";
      // 页面可见时才请求画面
      sendView(document.visibilityState !== "hidden");
      startPing();
    };

    ws.onmessage = (event) => {
      if (socket !== ws) return;
      if (typeof event.data === "string") handleText(event.data);
      else if (event.data instanceof Blob) handleFrame(event.data);
    };

    ws.onclose = () => {
      if (socket !== ws) return;
      socket = null;
      socketState.value = "closed";
      device.value = { ...device.value, online: false };
      stopPing();
      scheduleReconnect();
    };

    ws.onerror = () => {
      // 浏览器随后一定触发 close，这里不重复处理重连
    };
  }

  function reconnectNow() {
    if (disposed) return;
    clearReconnect();
    if (socket) {
      const stale = socket;
      socket = null;
      try {
        stale.close();
      } catch {
        // 忽略
      }
    }
    connect();
  }

  // ---------- 页面可见性 ----------

  function handleVisibility() {
    sendView(document.visibilityState !== "hidden");
  }

  onMounted(() => {
    document.addEventListener("visibilitychange", handleVisibility);
    connect();
  });

  onBeforeUnmount(() => {
    disposed = true;
    clearReconnect();
    stopPing();
    document.removeEventListener("visibilitychange", handleVisibility);
    if (socket) {
      const stale = socket;
      socket = null;
      stale.onclose = null;
      try {
        stale.close();
      } catch {
        // 忽略
      }
    }
    if (frameObjectUrl) {
      URL.revokeObjectURL(frameObjectUrl);
      frameObjectUrl = "";
    }
  });

  return {
    socketState,
    device,
    frameUrl,
    feedback,
    connected,
    sendCmd,
    sendView,
    reconnectNow,
  };
}

<script setup>
// 电脑端控制台：状态 + 画面 + 操作区
import { computed } from "vue";
import ControlPanel from "./components/ControlPanel.vue";
import ScreenPanel from "./components/ScreenPanel.vue";
import { useRemote } from "./composables/useRemote.js";

const {
  socketState,
  device,
  frameUrl,
  feedback,
  connected,
  sendCmd,
  reconnectNow,
} = useRemote();

const stateClass = computed(() => {
  if (connected.value) return "online";
  return socketState.value === "open" ? "offline" : "pending";
});

const stateLabel = computed(() => {
  if (connected.value) return "手机已连接";
  if (socketState.value === "open") return "等待手机连接";
  if (socketState.value === "connecting") return "连接服务器中…";
  return "与服务器断开，正在重连…";
});

const screenHint = computed(() => {
  if (socketState.value === "open") return "手机端 App 未连接";
  if (socketState.value === "connecting") return "正在连接服务器…";
  return "与服务器断开，正在重连…";
});

const deviceName = computed(() => device.value.name || "—");
const resolution = computed(() =>
  device.value.w > 0 && device.value.h > 0
    ? `${device.value.w} × ${device.value.h}`
    : "—",
);
const accessibility = computed(() => {
  if (device.value.accessibility === true) return "已开启";
  if (device.value.accessibility === false) return "未开启";
  return "—";
});

function onTap(point) {
  sendCmd("tap", point);
}

function onSwipe(gesture) {
  sendCmd("swipe", gesture);
}

function onLongpress(gesture) {
  sendCmd("longpress", gesture);
}

function onKey(name) {
  sendCmd("key", { name });
}

function onText(text) {
  sendCmd("text", { text });
}
</script>

<template>
  <main>
    <header class="head">
      <div>
        <h1>手机控制台</h1>
        <p class="sub">在浏览器里实时查看并操作手机</p>
      </div>
    </header>

    <div class="content">
      <section class="status" aria-live="polite">
        <span class="dot" :class="stateClass" aria-hidden="true"></span>
        <span class="state">{{ stateLabel }}</span>
        <button
          v-if="socketState === 'closed'"
          type="button"
          class="retry"
          @click="reconnectNow"
        >
          立即重连
        </button>
      </section>

      <dl class="meta">
        <div>
          <dt>设备</dt>
          <dd>{{ deviceName }}</dd>
        </div>
        <div>
          <dt>分辨率</dt>
          <dd>{{ resolution }}</dd>
        </div>
        <div>
          <dt>无障碍</dt>
          <dd>{{ accessibility }}</dd>
        </div>
      </dl>

      <ScreenPanel
        :frame-url="frameUrl"
        :online="connected"
        :width="device.w"
        :height="device.h"
        :hint="screenHint"
        @tap="onTap"
        @swipe="onSwipe"
        @longpress="onLongpress"
      />

      <ControlPanel :disabled="!connected" @key="onKey" @text="onText" />

      <p class="feedback" aria-live="polite">
        {{ feedback || "点击画面执行点击，拖动滑动，按住 600ms 长按。" }}
      </p>
    </div>
  </main>
</template>

<style scoped>
.status {
  display: flex;
  align-items: center;
  gap: 8px;
  min-height: 26px;
}

.dot {
  flex: none;
  width: 7px;
  height: 7px;
  border: 1px solid var(--muted);
  border-radius: 50%;
  background: transparent;
}

.dot.online {
  border-color: var(--fg);
  background: var(--fg);
}

.state {
  font-size: 13px;
}

.retry {
  margin-left: auto;
  padding: 3px 10px;
  border: 1px solid var(--line);
  border-radius: 6px;
  background: transparent;
  color: var(--fg);
  font: inherit;
  font-size: 12px;
  cursor: pointer;
}

.retry:hover {
  background: var(--hover);
}

.meta {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 8px;
  margin: 12px 0 14px;
  padding: 10px 0;
  border-top: 1px solid var(--line);
  border-bottom: 1px solid var(--line);
}

.meta div {
  min-width: 0;
}

.meta dt {
  color: var(--muted);
  font-size: 11px;
}

.meta dd {
  margin: 0;
  overflow: hidden;
  font-size: 13px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.feedback {
  margin: 14px 0 0;
  min-height: 20px;
  color: var(--muted);
  font-size: 12px;
}
</style>

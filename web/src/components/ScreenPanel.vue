<script setup>
// 画面区：<img> 显示设备 JPEG 帧，并把指针手势换算成归一化坐标指令
import { computed, ref } from "vue";

const LONG_PRESS_MS = 600;
const MOVE_TOLERANCE_PX = 8; // 判定拖动的手指抖动容差

const props = defineProps({
  frameUrl: { type: String, default: "" },
  online: { type: Boolean, default: false },
  width: { type: Number, default: 0 },
  height: { type: Number, default: 0 },
  hint: { type: String, default: "" },
});

const emit = defineEmits(["tap", "swipe", "longpress"]);

const surface = ref(null);
const active = ref(false);

let startPoint = null; // 归一化起点
let lastPoint = null; // 归一化最后一点
let startClient = null; // 起点像素，用于判断是否拖动
let startedAt = 0;
let moved = false;
let longPressTimer = null;
let longPressFired = false;

const aspectStyle = computed(() => {
  const w = props.width > 0 ? props.width : 9;
  const h = props.height > 0 ? props.height : 20;
  return { "--aspect": `${w} / ${h}`, "--ar": String(w / h) };
});

function roundRatio(value) {
  return Math.min(1, Math.max(0, Math.round(value * 10000) / 10000));
}

function normalize(event) {
  const el = surface.value;
  if (!el) return null;
  const rect = el.getBoundingClientRect();
  if (!rect.width || !rect.height) return null;
  return {
    x: roundRatio((event.clientX - rect.left) / rect.width),
    y: roundRatio((event.clientY - rect.top) / rect.height),
  };
}

function clearLongPressTimer() {
  if (longPressTimer !== null) {
    clearTimeout(longPressTimer);
    longPressTimer = null;
  }
}

function reset() {
  clearLongPressTimer();
  active.value = false;
  startPoint = null;
  lastPoint = null;
  startClient = null;
  moved = false;
  longPressFired = false;
}

function onPointerDown(event) {
  if (!props.online) return;
  if (event.pointerType === "mouse" && event.button !== 0) return;
  const point = normalize(event);
  if (!point) return;

  event.preventDefault();
  try {
    surface.value.setPointerCapture(event.pointerId);
  } catch {
    // 忽略：部分环境不支持指针捕获
  }

  active.value = true;
  startPoint = point;
  lastPoint = point;
  startClient = { x: event.clientX, y: event.clientY };
  startedAt = performance.now();
  moved = false;
  longPressFired = false;

  clearLongPressTimer();
  longPressTimer = setTimeout(() => {
    longPressTimer = null;
    if (!active.value || moved || !startPoint) return;
    longPressFired = true;
    emit("longpress", { x: startPoint.x, y: startPoint.y, ms: LONG_PRESS_MS });
  }, LONG_PRESS_MS);
}

function onPointerMove(event) {
  if (!active.value || !startClient) return;
  if (!moved) {
    const dx = event.clientX - startClient.x;
    const dy = event.clientY - startClient.y;
    if (Math.hypot(dx, dy) >= MOVE_TOLERANCE_PX) {
      moved = true;
      clearLongPressTimer();
    }
  }
  lastPoint = normalize(event) || lastPoint;
}

function onPointerUp(event) {
  if (!active.value || !startPoint) {
    reset();
    return;
  }
  const origin = startPoint;
  const end = normalize(event) || lastPoint || origin;
  const duration = Math.max(0, Math.round(performance.now() - startedAt));
  const wasLongPress = longPressFired;
  const wasMoved = moved;
  reset();

  if (wasLongPress) return; // 长按已在计时器里发出

  if (!wasMoved) {
    if (duration >= LONG_PRESS_MS) {
      emit("longpress", { x: origin.x, y: origin.y, ms: duration });
    } else {
      emit("tap", { x: origin.x, y: origin.y });
    }
    return;
  }

  emit("swipe", {
    x1: origin.x,
    y1: origin.y,
    x2: end.x,
    y2: end.y,
    ms: Math.max(1, duration),
  });
}

function onPointerCancel() {
  reset();
}
</script>

<template>
  <div class="screen-wrap">
    <div
      ref="surface"
      class="screen"
      :class="{ 'is-online': online, 'is-active': active }"
      :style="aspectStyle"
      @pointerdown="onPointerDown"
      @pointermove="onPointerMove"
      @pointerup="onPointerUp"
      @pointercancel="onPointerCancel"
      @lostpointercapture="onPointerCancel"
      @contextmenu.prevent
    >
      <img
        v-if="online && frameUrl"
        class="frame"
        :src="frameUrl"
        alt="手机实时画面"
        draggable="false"
      />
      <div v-else class="blank" role="status">
        <p class="blank-title">{{ online ? "等待画面…" : hint || "手机端 App 未连接" }}</p>
        <p class="blank-hint">
          {{ online ? "设备已连接，正在接收画面" : "在手机上打开 App 并开始投屏后可在此操作" }}
        </p>
      </div>
    </div>
  </div>
</template>

<style scoped>
.screen-wrap {
  display: flex;
  justify-content: center;
}

.screen {
  /* 画面高度上限：既不超过可视区比例，也保证下方操作区在常见窗口高度内可见 */
  --screen-max-h: clamp(240px, calc(100vh - 460px), 60vh);
  position: relative;
  width: min(100%, calc(var(--screen-max-h) * var(--ar)));
  aspect-ratio: var(--aspect);
  overflow: hidden;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: var(--bg);
  touch-action: none;
  user-select: none;
  -webkit-user-select: none;
}

.screen.is-online {
  cursor: crosshair;
}

.screen.is-active {
  border-color: var(--muted);
}

.frame {
  display: block;
  width: 100%;
  height: 100%;
  object-fit: contain;
  -webkit-user-drag: none;
  user-select: none;
  -webkit-user-select: none;
}

.blank {
  position: absolute;
  inset: 0;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 6px;
  padding: 16px;
  background: var(--bg);
  text-align: center;
}

.blank-title {
  margin: 0;
  color: var(--fg);
  font-size: 13px;
}

.blank-hint {
  margin: 0;
  color: var(--muted);
  font-size: 12px;
}

@media (max-width: 560px) {
  .screen {
    --screen-max-h: 52vh;
    border-radius: 6px;
  }

  .blank {
    gap: 4px;
    padding: 10px;
  }
}
</style>

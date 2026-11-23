<script setup>
// 操作区：按键条 + 文本输入
import { computed, ref } from "vue";

const props = defineProps({
  disabled: { type: Boolean, default: false },
});

const emit = defineEmits(["key", "text"]);

const KEYS = Object.freeze([
  { name: "back", label: "返回" },
  { name: "home", label: "桌面" },
  { name: "recents", label: "多任务" },
  { name: "notifications", label: "通知栏" },
  { name: "volume_up", label: "音量+" },
  { name: "volume_down", label: "音量-" },
  { name: "wake", label: "亮屏" },
  { name: "sleep", label: "锁屏" },
]);

const text = ref("");
const canSend = computed(() => !props.disabled && text.value.length > 0);

function pressKey(name) {
  if (props.disabled) return;
  emit("key", name);
}

function submitText() {
  if (!canSend.value) return;
  // 协议要求 ≤500 字，按码点截断
  emit("text", Array.from(text.value).slice(0, 500).join(""));
  text.value = "";
}

function onKeydown(event) {
  if (event.isComposing || event.key !== "Enter") return;
  event.preventDefault();
  submitText();
}
</script>

<template>
  <section class="controls" aria-label="操作区">
    <div class="keys">
      <button
        v-for="item in KEYS"
        :key="item.name"
        type="button"
        class="key"
        :disabled="disabled"
        @click="pressKey(item.name)"
      >
        {{ item.label }}
      </button>
    </div>

    <div class="text-row">
      <input
        v-model="text"
        class="text-input"
        type="text"
        maxlength="500"
        autocomplete="off"
        :disabled="disabled"
        placeholder="向手机输入文本，回车发送"
        aria-label="要发送到手机的文本"
        @keydown="onKeydown"
      />
      <button type="button" class="send" :disabled="!canSend" @click="submitText">
        发送
      </button>
    </div>
  </section>
</template>

<style scoped>
.controls {
  display: flex;
  flex-direction: column;
  gap: 10px;
  margin-top: 14px;
}

.keys {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 8px;
}

.key,
.send {
  padding: 9px 6px;
  border: 1px solid var(--line);
  border-radius: 6px;
  background: var(--hover);
  color: var(--fg);
  font: inherit;
  font-size: 13px;
  line-height: 1.4;
  cursor: pointer;
}

.key:hover:not(:disabled),
.send:hover:not(:disabled) {
  background: var(--line);
}

.key:active:not(:disabled),
.send:active:not(:disabled) {
  background: var(--line);
  color: var(--muted);
}

.key:disabled,
.send:disabled {
  color: var(--muted);
  opacity: 0.55;
  cursor: not-allowed;
}

.key:focus-visible,
.send:focus-visible,
.text-input:focus-visible {
  outline: 1px solid var(--muted);
  outline-offset: 1px;
}

.text-row {
  display: flex;
  gap: 8px;
}

.text-input {
  flex: 1;
  min-width: 0;
  padding: 9px 10px;
  border: 1px solid var(--line);
  border-radius: 6px;
  background: var(--hover);
  color: var(--fg);
  font: inherit;
  font-size: 13px;
  line-height: 1.4;
}

.text-input::placeholder {
  color: var(--muted);
}

.text-input:disabled {
  color: var(--muted);
  opacity: 0.7;
  cursor: not-allowed;
}

.send {
  flex: none;
  padding: 9px 18px;
}

@media (max-width: 560px) {
  .key {
    padding: 10px 4px;
    font-size: 12px;
  }
}
</style>

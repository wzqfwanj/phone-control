import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";

export default defineConfig({
  plugins: [vue()],
  // 相对路径产物：线上挂在 https://wkeeli.cn/control/ 下（Caddy 剥前缀后仍可用）
  base: "./",
  server: {
    // 仅本地开发：把浏览器的 ws/web 代理到本机服务端（不参与构建）
    proxy: {
      "/ws/web": {
        target: "ws://127.0.0.1:8090",
        ws: true,
      },
    },
  },
});

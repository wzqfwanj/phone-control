// 构建后把手机端 APK 放进 dist/，使控制台页面能直接下载（/control/phone-control.apk）。
//
// 查找顺序（可用环境变量 APK_SRC 显式指定）：
//   1. $APK_SRC
//   2. <repo>/dist/phone-control-personal.apk   （内置配对码，页面上"装完即用"）
//   3. <repo>/dist/phone-control-generic.apk    （通用版，需手填配对码）
// 都没有时只警告、不失败 —— 公开仓库里不含 APK，照样能构建页面。
import { copyFileSync, existsSync, mkdirSync, statSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const webDir = resolve(here, "..");
const repoRoot = resolve(webDir, "..");
const distDir = resolve(webDir, "dist");
const target = join(distDir, "phone-control.apk");

const candidates = [
  process.env.APK_SRC,
  join(repoRoot, "dist", "phone-control-personal.apk"),
  join(repoRoot, "dist", "phone-control-generic.apk"),
].filter(Boolean);

const source = candidates.find((p) => existsSync(p));
if (!source) {
  console.warn("[copy-apk] 没找到 APK（跳过）：设置 APK_SRC，或先构建 dist/phone-control-*.apk");
  process.exit(0);
}

mkdirSync(distDir, { recursive: true });
copyFileSync(source, target);
const size = statSync(target).size;
console.log(`[copy-apk] ${source} -> dist/phone-control.apk (${size} bytes)`);

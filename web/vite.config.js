import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";
import { fileURLToPath } from "node:url";

export default defineConfig({
  plugins: [vue()],
  server: {
    proxy: { "/api": "http://127.0.0.1:8765" },
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
    rollupOptions: {
      input: {
        main: fileURLToPath(new URL("./index.html", import.meta.url)),
        admin: fileURLToPath(new URL("./admin.html", import.meta.url)),
        "pwd-lab": fileURLToPath(new URL("./pwd-lab.html", import.meta.url)),
      },
      // 文件名不带内容哈希。web/dist 要提交进仓库，哈希一变就是「旧文件被改、新文件被删」，
      // 两条都动过页面的分支在 GitHub 上必定冲突。固定名字后只剩同一文件的内容冲突。
      output: {
        entryFileNames: "assets/[name].js",
        chunkFileNames: "assets/[name].js",
        assetFileNames: "assets/[name][extname]",
      },
    },
  },
});

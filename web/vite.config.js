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
      // 文件名不带内容哈希。web/dist 由 update_tankstorm.sh 在服务器上生成，不进仓库。
      // 固定名字后，每次构建覆盖同一批文件，不会留下过期的哈希文件。
      output: {
        entryFileNames: "assets/[name].js",
        chunkFileNames: "assets/[name].js",
        assetFileNames: "assets/[name][extname]",
      },
    },
  },
});

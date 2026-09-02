import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import AutoImport from 'unplugin-auto-import/vite'
import Components from 'unplugin-vue-components/vite'
import { ElementPlusResolver } from 'unplugin-vue-components/resolvers'

// Vite 配置。
// 开发：dev server 跑 :5173，把 /api 代理到 FastAPI :8000（前后端分离）。
// 生产：npm run build → dist/，FastAPI 在根路径托管（见 src/bycall/api/app.py _mount_spa）。
export default defineConfig({
  plugins: [
    vue(),
    // Element Plus 按需引入：自动导入组件（el-*）和 API（ElMessage 等）
    AutoImport({ resolvers: [ElementPlusResolver()] }),
    Components({ resolvers: [ElementPlusResolver()] }),
  ],
  server: {
    port: 5173,
    // 双栈监听（127.0.0.1 + ::1）：默认 'localhost' 在 Node ≥17 会被解析为仅 ::1，
    // 导致浏览器走 127.0.0.1 时连接被拒（页面"打不开"）。
    host: true,
    proxy: {
      // uvicorn 只绑 IPv4 127.0.0.1:8000；目标显式写 127.0.0.1，
      // 不依赖 localhost 的解析顺序（Node/macOS 下 ::1 与 127.0.0.1 顺序不稳定）。
      '/api': { target: 'http://127.0.0.1:8000', changeOrigin: false },
      '/chat': { target: 'http://127.0.0.1:8000', changeOrigin: false },
    },
  },
  build: {
    outDir: 'dist',
    // 用相对 base，便于 FastAPI 在任意路径托管。
    base: './',
    rollupOptions: {
      output: {
        // 只把稳定的框架层拆为 vendor（缓存友好）；
        // Element Plus 由 unplugin 按组件自然分包，不强制合并。
        manualChunks(id) {
          if (id.includes('node_modules')) {
            if (id.includes('/vue/') || id.includes('/@vue/') || id.includes('/vue-router/') || id.includes('/axios/')) {
              return 'vendor'
            }
          }
        },
      },
    },
  },
})

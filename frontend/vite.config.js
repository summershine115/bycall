import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// Vite 配置。
// 开发：dev server 跑 :5173，把 /api 代理到 FastAPI :8000（前后端分离）。
// 生产：npm run build → dist/，FastAPI 在根路径托管（见 src/bycall/api/app.py _mount_spa）。
export default defineConfig({
  plugins: [vue()],
  server: {
    port: 5173,
    proxy: {
      '/api': 'http://localhost:8000',
      '/chat': 'http://localhost:8000',
    },
  },
  build: {
    outDir: 'dist',
    // 用相对 base，便于 FastAPI 在任意路径托管。
    base: './',
  },
})

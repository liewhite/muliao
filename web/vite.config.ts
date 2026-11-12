import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// 开发时把 API 转发到后端（默认 127.0.0.1:8000）
const backend = 'http://127.0.0.1:8000'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  // 构建产物在 web/dist（不提交）：服务用 --web-dir 指向它，安装时放到哪由安装脚本定
  server: {
    proxy: {
      '/api': { target: backend, changeOrigin: false },
    },
  },
})

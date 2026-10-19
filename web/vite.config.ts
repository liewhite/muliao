import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// 开发时把 API 转发到后端（默认 127.0.0.1:8000）
const backend = 'http://127.0.0.1:8000'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  // 构建产物放进 Python 包，随 wheel 分发：安装端不需要 node（见 docs/deployment.md）
  build: {
    outDir: '../src/muliao/webui',
    emptyOutDir: true,
  },
  server: {
    proxy: {
      '/api': { target: backend, changeOrigin: false },
    },
  },
})

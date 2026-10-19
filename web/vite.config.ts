import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// 开发时把 API、webhook 转发到后端（MULIAO_PORT 默认 8000）
const backend = 'http://127.0.0.1:8000'

export default defineConfig({
  plugins: [react()],
  // 构建产物放进 Python 包，随 wheel 分发：安装端不需要 node（见 docs/deployment.md）
  build: {
    outDir: '../src/muliao/webui',
    emptyOutDir: true,
  },
  server: {
    proxy: {
      '/api': { target: backend, changeOrigin: false },
      '/hooks': { target: backend, changeOrigin: false },
    },
  },
})

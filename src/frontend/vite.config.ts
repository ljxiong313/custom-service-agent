// Vite 配置：/api 代理到本地后端 7861（同 AgentChat 的开发模式），
// SSE 走同一代理，需在 http.ts/chat.ts 中不做额外缓冲。
import { fileURLToPath, URL } from 'node:url'
import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) },
  },
  server: {
    port: 8091,
    proxy: {
      '/api': {
        target: 'http://localhost:7861',
        changeOrigin: true,
      },
    },
  },
})

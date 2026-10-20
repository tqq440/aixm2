import { fileURLToPath, URL } from 'node:url'
import react from '@vitejs/plugin-react'
import { loadEnv } from 'vite'
import { defineConfig } from 'vitest/config'

export function localBackendTarget(value: string): string {
  let target: URL
  try {
    target = new URL(value)
  } catch {
    throw new Error('INFERENCE_API_TARGET 必须是本机 HTTP 地址，例如 http://127.0.0.1:5000。')
  }
  if (
    !['http:', 'https:'].includes(target.protocol)
    || !['127.0.0.1', 'localhost', '[::1]'].includes(target.hostname)
    || target.username || target.password
    || target.pathname !== '/' || target.search || target.hash
  ) {
    throw new Error('INFERENCE_API_TARGET 仅允许无路径、凭证和查询参数的本机回环地址。')
  }
  return target.origin
}

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), 'INFERENCE_')
  const target = localBackendTarget(env.INFERENCE_API_TARGET || 'http://127.0.0.1:5000')
  const proxy = { '/api': { target, changeOrigin: false } }
  return {
    plugins: [react()],
    resolve: { alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) } },
    server: { host: '127.0.0.1', port: 5173, strictPort: true, proxy },
    preview: { host: '127.0.0.1', port: 4173, strictPort: true, proxy },
    test: {
      environment: 'jsdom',
      globals: true,
      include: ['src/**/*.test.{ts,tsx}'],
      clearMocks: true,
      restoreMocks: true,
    },
  }
})

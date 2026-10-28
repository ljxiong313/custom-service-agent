// 前端单测配置（CI 里 `npm run test` 读这一份，不读 vite.config.ts）。
//
// 刻意与 vite.config.ts 分离：那份里的 /api → localhost:7861 代理是给 dev server 用的，
// 单测全部离线跑（fetch 被 vi.stubGlobal 替掉），不应该也不需要真后端在场。
// 这里只保留两件事：vue 插件（SFC 要能编译）+ `@` 别名（源码里满屏 @/xxx）。
//
// globals 关掉：测试文件一律显式 `import { describe, it, expect, vi } from 'vitest'`，
// 这样 vue-tsc 的严格模式下不需要额外注入 vitest 全局类型，也不会和 DOM 全局重名混淆。
import { fileURLToPath, URL } from 'node:url'
import { defineConfig } from 'vitest/config'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) },
  },
  test: {
    environment: 'jsdom',
    globals: false,
    // 测试与源码同目录放置（src/**\/*.spec.ts），这样 tsconfig.json 的 include
    // 天然覆盖到它们，`npm run typecheck`/build 前的 vue-tsc 会一并做类型检查。
    include: ['src/**/*.spec.ts'],
    // 每个用例结束后自动恢复 mock 的实现，避免跨文件的 fetch/store 桩互相污染
    restoreMocks: true,
    // 缓存在本机必须落在项目内：这台 Windows 上 vitest 往 %LOCALAPPDATA%\Temp 写
    // 会撞 EACCES（`vitest run` 直接起不来，只能加 --root 绕）。CI 的 Linux 无此问题，
    // 但默认值统一更省事——见 docs/运维手册.md 的"本地前端测试"一节。
    tempDir: 'node_modules/.vitest/tmp',
  },
  cacheDir: 'node_modules/.vitest/cache',
})

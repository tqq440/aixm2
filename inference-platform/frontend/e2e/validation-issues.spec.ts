import { test, expect } from '@playwright/test'

for (const width of [1366, 390]) test(`validation issues ${width}px reads only cached local records`, async ({ page }, testInfo) => {
  await page.setViewportSize({ width, height: 900 })
  const calls: string[] = [], errors: string[] = []
  page.on('pageerror', error => errors.push(error.message))
  await page.route(url => url.pathname.startsWith('/api/'), async route => {
    const request = route.request(), path = new URL(request.url()).pathname
    calls.push(`${request.method()} ${path}`)
    const ok = (data: unknown) => route.fulfill({ status: 200, json: { data, requestId: 'isolated-validation-browser' } })
    if (path === '/api/admin/gateway/status') return ok({ enabled: false, managementAvailable: false, notice: '公共调用尚未启用' })
    if (path === '/api/admin/validation/issues') return ok({
      items: [{ stage: 'module6b', target: 'swufefic-v100', code: 'CAMPUS_NETWORK_UNAVAILABLE', source: 'operator', state: 'waiting_external_verification', firstObservedAt: '2026-10-22T01:00:00Z', lastObservedAt: '2026-10-22T02:00:00Z', observationCount: 1, evidenceSha256: 'a'.repeat(64) }],
      total: 1, offset: 0, limit: 50, unreadableCount: 1, scanTruncated: false, notice: '仅本地记录',
    })
    return route.fulfill({ status: 503, json: { error: { code: 'ISOLATED_LEGACY', message: '隔离页面不连接日常后台', details: [] }, requestId: 'isolated-validation' } })
  })
  await page.goto('/')
  await page.getByRole('button', { name: '应用与任务监控', exact: true }).click()
  await expect(page.getByRole('heading', { name: '环境与待验收记录' })).toBeVisible()
  await expect(page.getByText('swufefic-v100', { exact: true })).toBeVisible()
  await expect(page.getByText('待外部验收', { exact: true })).toBeVisible()
  await expect(page.getByText(/有 1 份记录无法读取/)).toBeVisible()
  await page.getByText('查看记录信息', { exact: true }).click()
  await expect(page.getByText('a'.repeat(64), { exact: true })).toBeVisible()
  // 开发版StrictMode可能重挂载；只要求一次手动刷新增加一次本地GET。
  const readsBeforeRefresh = calls.filter(call => call === 'GET /api/admin/validation/issues').length
  await page.getByRole('button', { name: '刷新验收记录', exact: true }).click()
  await expect.poll(() => calls.filter(call => call === 'GET /api/admin/validation/issues').length).toBe(readsBeforeRefresh + 1)
  expect(calls.every(call => call.startsWith('GET '))).toBe(true)
  expect(calls.some(call => call.includes('/nodes/refresh'))).toBe(false)
  expect(errors).toEqual([])
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
  expect(await page.evaluate(() => localStorage.length)).toBe(0)
  await page.screenshot({ path: testInfo.outputPath(`validation-issues-${width}.png`), fullPage: true })
})

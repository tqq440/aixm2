import { test, expect, type Page } from '@playwright/test'
import { catalogEntry, catalogGroups, catalogPage } from '../src/test/serviceCatalogFixtures'
import type { CatalogEntry } from '../src/types/serviceCatalog'

test.setTimeout(45_000)

async function fixture(page: Page, initial: CatalogEntry[] = []) {
  const entries = structuredClone(initial)
  const calls: string[] = []
  const errors: string[] = []
  page.on('pageerror', error => errors.push(error.message))
  await page.route(url => url.pathname.startsWith('/api/'), async route => {
    const request = route.request(), path = new URL(request.url()).pathname
    calls.push(`${request.method()} ${path}`)
    const ok = (data: unknown) => route.fulfill({ status: 200, json: { data, requestId: 'catalog-isolated-browser' } })
    if (!path.startsWith('/api/admin/service-catalog')) return route.fulfill({ status: 503,
      json: { error: { code: 'ISOLATED_LEGACY', message: '隔离目录浏览器不连接旧后台', details: [] }, requestId: 'catalog-isolated-browser' } })
    if (path.endsWith('/resource-groups')) return ok(catalogGroups())
    if (path.endsWith('/services') && request.method() === 'GET') return ok(catalogPage(entries))
    const id = path.split('/services/')[1]?.split('/')[0]
    if (request.method() === 'PUT') {
      const body = request.postDataJSON()
      const old = entries.findIndex(item => item.serviceId === id)
      const entry = { ...catalogEntry(), serviceId: id, config: body.config, version: old < 0 ? 1 : entries[old].version + 1 }
      if (old < 0) entries.push(entry); else entries[old] = entry
      return ok(entry)
    }
    const entry = entries.find(item => item.serviceId === id)
    if (entry && path.endsWith('/test')) return ok({ success: true, errorCode: null, message: '隔离健康检查通过；未验证推理或设备。' })
    if (entry && path.endsWith('/activation')) {
      entry.enabled = request.postDataJSON().enabled
      entry.activeRevision = entry.draftRevision; entry.version++
      return ok(entry)
    }
    if (entry && request.method() === 'GET') return ok(entry)
    throw new Error(`Unexpected catalog request: ${request.method()} ${path}`)
  })
  await page.goto('/')
  await page.getByRole('button', { name: '服务管理', exact: true }).click()
  await expect(page.getByRole('heading', { name: '公共服务目录', exact: true })).toBeVisible()
  await expect(page.getByRole('button', { name: '保存草稿', exact: true })).toBeEnabled()
  return { entries, calls, errors }
}

test('register vision, explicitly test and enable only the catalog', async ({ page }, testInfo) => {
  const state = await fixture(page)
  const catalog = page.getByRole('region', { name: '公共服务目录', exact: true })
  await catalog.getByRole('combobox', { name: '服务类型', exact: true }).selectOption('vision')
  await catalog.getByLabel('服务编号', { exact: true }).fill('lab-yolo')
  await catalog.getByLabel('服务名称', { exact: true }).fill('实验室图片检测')
  await catalog.getByLabel('平台模型编号', { exact: true }).fill('yolo-custom')
  await catalog.getByLabel('服务地址', { exact: true }).fill('http://127.0.0.1:8012')
  await catalog.getByLabel('上游模型名', { exact: true }).fill('yolo-custom')
  await page.getByRole('button', { name: '保存草稿', exact: true }).click()
  await expect(page.getByRole('button', { name: /实验室图片检测/ })).toBeVisible()
  expect(state.entries[0].config.kind).toBe('vision')
  expect(state.entries[0].config.targets[0]).not.toHaveProperty('protocol')
  await page.getByRole('button', { name: '测试此目标连接', exact: true }).click()
  await expect(page.getByText('隔离健康检查通过；未验证推理或设备。')).toBeVisible()
  await page.getByRole('button', { name: '启用目录修订', exact: true }).click()
  await expect(page.getByRole('button', { name: '停用目录服务', exact: true })).toBeVisible()
  await page.screenshot({ path: testInfo.outputPath('catalog-desktop.png'), fullPage: true })
  expect(state.calls.filter(call => /POST|PUT|DELETE/.test(call)).every(call => call.includes('/admin/service-catalog/'))).toBe(true)
  expect(state.errors).toEqual([])
})

test('missing progress and actual device remain declarations on mobile', async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 390, height: 844 })
  const state = await fixture(page, [catalogEntry()])
  await page.getByRole('button', { name: /示例语音服务/ }).click()
  await expect(page.getByText(/服务未提供进度；不推算百分比/)).toBeVisible()
  await expect(page.getByRole('combobox', { name: '目标设备资料', exact: true })).toHaveValue('unknown')
  await expect(page.getByText(/公共执行层尚未接入；本目录不会用于当前任务/)).toBeVisible()
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1)).toBe(true)
  await page.screenshot({ path: testInfo.outputPath('catalog-mobile.png'), fullPage: true })
  expect(state.calls.filter(call => /POST|PUT|DELETE/.test(call))).toEqual([])
  expect(state.errors).toEqual([])
})

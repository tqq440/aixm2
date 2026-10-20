import { expect, test, type Page } from '@playwright/test'
import { mkdir, readFile, writeFile } from 'node:fs/promises'
import path from 'node:path'
import { llmModel, llmStatus, onlineRecord } from '../src/test/llmFixtures'
import type { LlmEvent, OnlineRequestView } from '../src/types/llm'

const evidence = () => path.resolve(process.cwd(), process.env.E2E_EVIDENCE_DIR ?? '../runtime/verification/part12')
const frame = (event: LlmEvent) => `event: ${event.type}\ndata: ${JSON.stringify(event)}\n\n`

async function fixture(page: Page, failure = true) {
  let saved: OnlineRequestView | null = null
  let posts = 0, cancellations = 0
  await page.route(url => url.pathname.startsWith('/api/'), async route => {
    const request = route.request(), url = new URL(request.url())
    let data: unknown
    if (url.pathname === '/api/llm/status') data = llmStatus
    else if (url.pathname === '/api/models') data = { items: url.searchParams.get('taskType') === 'llm' ? [llmModel] : [] }
    else if (url.pathname === '/api/llm/requests' && request.method() === 'POST') {
      posts += 1
      const input = request.postDataJSON()
      saved = onlineRecord({ onlineRequestId: input.onlineRequestId, state: failure ? 'failed' : 'streaming', permitState: failure ? 'released' : 'held', outputCharacters: 8 })
      const common = { onlineRequestId: saved.onlineRequestId, requestId: 'mock-browser-request' }
      let body = frame({ ...common, type: 'accepted', sequence: 1, record: { ...saved, state: 'accepted' } })
        + frame({ ...common, type: 'delta', sequence: 2, text: '协议测试的部分文字' })
      if (failure) body += frame({ ...common, type: 'error', sequence: 3, record: saved, error: { code: 'FIXTURE_INTERRUPTED', message: '浏览器协议测试模拟流中断；不代表真实模型输出。' } })
      await route.fulfill({ status: 200, contentType: 'text/event-stream', headers: { 'X-Request-ID': 'mock-browser-request' }, body }); return
    } else if (url.pathname.endsWith('/cancel') && saved) {
      cancellations += 1
      saved = { ...saved, state: 'streaming', cancelRequested: true, cancellationConfirmed: false }
      data = { request: saved, decision: 'cancel_requested' }
    } else if (url.pathname === '/api/llm/requests') data = { items: saved ? [saved] : [], total: saved ? 1 : 0, page: 1, pageSize: 20 }
    else if (url.pathname.startsWith('/api/llm/requests/') && saved) data = saved
    else if (url.pathname === '/api/tasks') data = { items: [], total: 0, page: 1, pageSize: 20 }
    else { await route.fulfill({ status: 503, contentType: 'application/json', body: JSON.stringify({ error: { code: 'TEST_ONLY', message: '模拟测试未启用该接口' }, requestId: 'mock-other' }) }); return }
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ data, requestId: 'mock-read' }) })
  })
  return { posts: () => posts, cancellations: () => cancellations, finishCancel: () => { if (saved) saved = { ...saved, state: 'cancelled', permitState: 'released', cancellationConfirmed: true } } }
}

test('模拟LLM：部分输出失败、下载和刷新不自动重放', async ({ page }) => {
  const backend = await fixture(page)
  await page.goto('/?type=llm')
  await page.getByLabel('输入内容', { exact: true }).fill('这是浏览器协议测试输入')
  await page.getByRole('button', { name: '开始生成', exact: true }).click()
  const output = page.getByRole('region', { name: '在线生成结果', exact: true })
  await expect(output.getByTestId('llm-response-text')).toHaveText('协议测试的部分文字')
  await expect(output).toContainText('本次输出不完整')
  await expect(output).toContainText('mock-browser-request')
  const requestId = new URL(page.url()).searchParams.get('request')!
  expect(requestId).toMatch(/^[0-9a-f]{32}$/)
  const downloadEvent = page.waitForEvent('download')
  await output.getByRole('button', { name: '下载部分 TXT', exact: true }).click()
  const download = await downloadEvent
  expect(download.suggestedFilename()).toBe(`llm-${requestId.slice(0, 8)}-partial.txt`)
  expect(await readFile((await download.path())!, 'utf8')).toBe('协议测试的部分文字')
  await page.getByRole('button', { name: '刷新状态', exact: true }).click()
  expect(backend.posts()).toBe(1)
  await mkdir(evidence(), { recursive: true })
  await page.screenshot({ path: path.join(evidence(), 'llm-mock-partial.png'), fullPage: true })
  await page.reload()
  await expect(output).toContainText('这里只恢复了请求状态')
  await expect(output.getByTestId('llm-response-text')).toHaveCount(0)
  expect(backend.posts()).toBe(1)
  await writeFile(path.join(evidence(), 'llm-mock-partial.json'), JSON.stringify({ evidenceKind: 'browser-protocol-fixture-not-model-execution', modelVerified: false, passed: true, posts: backend.posts(), requestId }, null, 2))
})

test('模拟LLM：停止受理与引擎确认分开显示', async ({ page }) => {
  const backend = await fixture(page, false)
  await page.goto('/?type=llm')
  await page.getByLabel('输入内容', { exact: true }).fill('模拟停止请求')
  await page.getByRole('button', { name: '开始生成', exact: true }).click()
  const output = page.getByRole('region', { name: '在线生成结果', exact: true })
  await expect(output).toContainText('未收到最终事件')
  await output.getByRole('button', { name: '停止生成', exact: true }).click()
  await expect(output.getByRole('button', { name: '停止请求已受理', exact: true })).toBeDisabled()
  await output.locator('summary').filter({ hasText: '请求与执行信息' }).click()
  await expect(output.getByText('尚未确认', { exact: true })).toBeVisible()
  await expect(output.getByText('占用中', { exact: true })).toBeVisible()
  expect(backend.cancellations()).toBe(1)
  backend.finishCancel()
  await page.getByRole('button', { name: '刷新状态', exact: true }).click()
  await expect(output.getByText('已取消', { exact: true })).toBeVisible()
  await expect(output.getByText('后台已确认', { exact: true })).toBeVisible()
  expect(backend.posts()).toBe(1)
})

test('真实接口只读：未配置LLM不可生成，三视口与旧工作台入口可用', async ({ page, request }) => {
  test.skip(process.env.E2E_LLM_STATUS !== '1', 'Only run against an explicitly started local platform; no generation requests are sent.')
  const status = (await (await request.get('/api/llm/status')).json()).data
  expect(status.configured).toBe(false); expect(status.serviceVerified).toBe(false)
  expect((await (await request.get('/api/models?taskType=llm')).json()).data.items).toEqual([])
  let generationPosts = 0
  page.on('request', current => { if (current.method() === 'POST' && new URL(current.url()).pathname === '/api/llm/requests') generationPosts += 1 })
  const errors: string[] = []; page.on('pageerror', error => errors.push(error.message))
  await page.goto('/?type=llm')
  await expect(page.getByRole('heading', { name: '尚未配置大模型服务', exact: true })).toBeVisible()
  await page.getByLabel('输入内容', { exact: true }).fill('仅验证按钮禁用，不发送生成请求。')
  await expect(page.getByRole('button', { name: '开始生成', exact: true })).toBeDisabled()
  await expect(page.getByTestId('llm-response-text')).toHaveCount(0)
  await mkdir(evidence(), { recursive: true })
  for (const [width, height] of [[1920, 1080], [1366, 768], [390, 844]]) {
    await page.setViewportSize({ width, height })
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
    await page.screenshot({ path: path.join(evidence(), `llm-unconfigured-${width}.png`), fullPage: true })
  }
  await page.getByRole('navigation', { name: '主导航' }).getByRole('button', { name: '音频转写', exact: true }).click()
  await expect(page.getByRole('heading', { name: '新建转写任务', exact: true })).toBeVisible()
  await page.getByRole('navigation', { name: '主导航' }).getByRole('button', { name: '图片检测', exact: true }).click()
  await expect(page.getByRole('heading', { name: '新建图片检测任务', exact: true })).toBeVisible()
  await page.getByRole('navigation', { name: '主导航' }).getByRole('button', { name: '文本生成', exact: true }).click()
  await expect(page.getByLabel('输入内容', { exact: true })).toHaveValue('仅验证按钮禁用，不发送生成请求。')
  expect(generationPosts).toBe(0); expect(errors).toEqual([])
  await writeFile(path.join(evidence(), 'llm-unconfigured-browser.json'), JSON.stringify({ evidenceKind: 'real-local-http-unconfigured-state-only', modelVerified: false, generationPosts, configured: status.configured, viewportWidths: [1920, 1366, 390], pageErrors: errors, passed: true }, null, 2))
})

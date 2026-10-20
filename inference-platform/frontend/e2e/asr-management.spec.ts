import { expect, test } from '@playwright/test'
import { execFileSync } from 'node:child_process'
import { mkdir, readFile, writeFile } from 'node:fs/promises'
import path from 'node:path'
import type { ManagementStatus } from '../src/types/management'

// Real management requests carry a secret. Never record traces, request headers,
// fill arguments, or credentials in attachments/reports.
test.use({ trace: 'off', video: 'off', screenshot: 'off' })
const out = path.resolve(process.cwd(), process.env.E2E_EVIDENCE_DIR ?? '../runtime/verification/asr3a')
const base = path.resolve(process.cwd(), '..')

test('真实管理页面：启动就绪、上传兼容、独立停止、历史结果与其他工作台', async ({ page, request }) => {
  test.skip(process.env.E2E_ASR_MANAGEMENT_REAL !== '1', '明确启用真实本机管理验收才运行；会创建一个真实转写任务。')
  await mkdir(out, { recursive: true })
  const token = execFileSync(process.env.E2E_PYTHON ?? 'C:/Users/86199/miniconda3/envs/project311/python.exe', [
    '-c', 'from backend.config.settings.app_settings import load_settings; print(load_settings().agent_token.get_secret_value())',
  ], { cwd: base, encoding: 'utf8', windowsHide: true }).trim()
  if (token.length < 32) throw new Error('本机管理令牌配置不可用')
  const pageErrors: string[] = []
  page.on('pageerror', error => pageErrors.push(error.message))
  const report: Record<string, unknown> = { kind: 'real-browser-real-agent-real-whisper', noNetworkMocking: true, startedAt: new Date().toISOString() }
  await page.goto('/')
  const panel = page.getByRole('region', { name: 'ASR 服务管理', exact: true })
  const state = panel.locator('.asr-service-state')
  await expect(state).toHaveText('未启动')
  await expect(panel.getByLabel('计算节点')).toHaveValue(/^[a-f0-9]{32}$/)
  const instanceId = await panel.getByLabel('ASR 实例', { exact: true }).inputValue()
  const status = async () => (await (await request.get(`/api/instances/${instanceId}/management`)).json()).data as ManagementStatus
  report.before = await status()
  await page.getByLabel('选择音频', { exact: true }).setInputFiles(path.join(base, 'runtime/samples/asr_example_zh.wav'))
  await expect(page.getByRole('button', { name: '提交转写', exact: true })).toBeDisabled()
  await expect(panel.getByRole('button', { name: '启动 ASR', exact: true })).toBeDisabled()
  // Invalid identity does not change desired state or command revision.
  await panel.getByLabel('本机管理令牌', { exact: true }).fill('invalid-test-token-'.repeat(3))
  const rejected = page.waitForResponse(r => r.url().endsWith('/management/start') && r.request().method() === 'POST')
  await panel.getByRole('button', { name: '启动 ASR', exact: true }).click()
  expect((await rejected).status()).toBe(401)
  await expect(state).toHaveText('未启动')
  expect((await status()).commandRevision).toBe((report.before as ManagementStatus).commandRevision)
  await panel.getByLabel('本机管理令牌', { exact: true }).fill(token)
  const start = page.waitForResponse(r => r.url().endsWith('/management/start') && r.request().method() === 'POST')
  await panel.getByRole('button', { name: '启动 ASR', exact: true }).click()
  expect((await start).status()).toBe(202)
  await expect(state).toHaveText('可以转写', { timeout: 120_000 })
  report.ready = await status()
  expect((report.ready as ManagementStatus).modelReady).toBe(true)
  await expect(page.getByRole('button', { name: '提交转写', exact: true })).toBeEnabled()
  const accepted = page.waitForResponse(r => r.url().endsWith('/api/tasks/asr') && r.request().method() === 'POST')
  await page.getByRole('button', { name: '提交转写', exact: true }).click()
  const response = await accepted
  expect(response.status()).toBe(201)
  const receipt = (await response.json()).data
  report.taskId = receipt.task.taskId
  await expect(page.getByRole('tab', { name: '转写全文', exact: true })).toBeVisible({ timeout: 120_000 })
  const result = (await (await request.get(`/api/tasks/${receipt.task.taskId}/result`)).json()).data
  report.output = result.output
  await expect(page.getByRole('tabpanel')).toContainText(result.output.text)
  for (const viewport of [{ width: 1920, height: 1080 }, { width: 1366, height: 768 }, { width: 390, height: 844 }]) {
    await page.setViewportSize(viewport)
    await panel.scrollIntoViewIfNeeded()
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
    await page.screenshot({ path: path.join(out, `asr-management-${viewport.width}.png`), fullPage: true })
  }
  await page.setViewportSize({ width: 1366, height: 768 })
  const stopping = page.waitForResponse(r => r.url().endsWith('/management/stop') && r.request().method() === 'POST')
  await panel.getByRole('button', { name: '停止 ASR', exact: true }).click()
  expect((await stopping).status()).toBe(202)
  await expect(state).toHaveText('未启动', { timeout: 60_000 })
  report.stopped = await status()
  expect((report.stopped as ManagementStatus).exitConfirmedAt).toBeTruthy()
  // Existing results remain downloadable with the ASR process stopped.
  const downloaded = page.waitForEvent('download')
  await page.getByRole('button', { name: '下载 TXT', exact: true }).click()
  const download = await downloaded
  expect(await readFile((await download.path())!, 'utf8')).toBe(result.output.text)
  await download.saveAs(path.join(out, 'asr-result.txt'))
  await page.reload()
  await expect(state).toHaveText('未启动')
  await expect(panel.getByLabel('本机管理令牌', { exact: true })).toHaveValue('')
  await expect(page.getByRole('tabpanel')).toContainText(result.output.text)
  await panel.getByLabel('本机管理令牌', { exact: true }).fill(token)
  const nav = page.getByRole('navigation', { name: '主导航' })
  await nav.getByRole('button', { name: '图片检测', exact: true }).click()
  await expect(page.getByRole('heading', { name: '新建图片检测任务', exact: true })).toBeVisible()
  await expect(page.getByRole('option', { name: 'YOLO · 通用目标检测' })).toHaveCount(1)
  report.vision = (await (await request.get('/api/runtime')).json()).data.visionWorker
  expect((report.vision as { modelReady: boolean }).modelReady).toBe(true)
  await nav.getByRole('button', { name: '文本生成', exact: true }).click()
  await expect(page.getByRole('button', { name: '开始生成', exact: true })).toBeDisabled()
  report.llm = (await (await request.get('/api/llm/status')).json()).data
  await nav.getByRole('button', { name: '音频转写', exact: true }).click()
  await expect(panel.getByLabel('本机管理令牌', { exact: true })).toHaveValue('')
  await expect(state).toHaveText('未启动')
  const stores = await page.evaluate(() => [JSON.stringify(localStorage), JSON.stringify(sessionStorage), window.location.href])
  if (stores.some(value => value.includes(token))) throw new Error('管理令牌泄漏到浏览器持久化存储或地址')
  report.tokenMemoryOnly = true
  report.pageErrors = pageErrors
  expect(pageErrors).toEqual([])
  report.completedAt = new Date().toISOString()
  await writeFile(path.join(out, 'browser-real.json'), JSON.stringify(report, null, 2), 'utf8')
})

test('模拟管理异常：加载/忙碌/停止未确认/失败/断网，不声称真实模型故障', async ({ page }) => {
  const nodeId = 'a'.repeat(32), instanceId = 'b'.repeat(32)
  let state: ManagementStatus['state'] = 'starting', unavailable = false
  await page.route('**/api/nodes?*', route => route.fulfill({ json: { data: { items: [{ nodeId, hostname: '模拟节点', osName: 'Windows', osVersion: 'test', architecture: 'AMD64', cpuCount: 1, gpuStatus: 'not_probed', registrationState: 'registered', agentStatus: 'online', registeredAt: new Date().toISOString(), metadataObservedAt: new Date().toISOString() }], total: 1, page: 1, pageSize: 100 }, requestId: 'test' } }))
  await page.route('**/api/instances?*', route => route.fulfill({ json: { data: { items: [{ instanceId, nodeId, instanceKey: 'asr-default', modelId: 'whisper-small', modelVersion: 'fixture', adapterKey: 'faster-whisper', executionDevice: 'cpu', registrationState: 'registered', runtimeState: 'unknown', managementMode: 'node_agent', registeredAt: new Date().toISOString() }], total: 1, page: 1, pageSize: 100 }, requestId: 'test' } }))
  await page.route(`**/api/instances/${instanceId}/management`, route => unavailable ? route.abort() : route.fulfill({ json: { data: { instanceId, nodeId, managementMode: 'node_agent', state, reportedState: state, desiredState: ['stop_unconfirmed', 'failed'].includes(state) ? 'stopped' : 'running', agentAvailable: true, agentSessionId: 'c'.repeat(32), agentPid: 123, workerSessionId: null, childPid: null, commandRevision: 1, modelReady: state === 'busy', acceptingTasks: state === 'busy', activeTaskId: state === 'busy' ? 'd'.repeat(32) : null, lastError: state === 'failed' ? '模拟模型加载失败' : null, heartbeatAt: new Date().toISOString(), evaluatedAt: new Date().toISOString(), exitConfirmedAt: null, shutdownConfirmedAt: null }, requestId: 'test' } }))
  await page.goto('/')
  const panel = page.getByRole('region', { name: 'ASR 服务管理', exact: true })
  const badge = panel.locator('.asr-service-state')
  await expect(badge).toHaveText('模型加载中')
  state = 'busy'; await panel.getByRole('button', { name: '刷新服务状态' }).click()
  await expect(badge).toHaveText('正在执行')
  await expect(panel.getByRole('button', { name: /查看当前任务/ })).toBeVisible()
  state = 'stop_unconfirmed'; await panel.getByRole('button', { name: '刷新服务状态' }).click()
  await expect(badge).toHaveText('停止未确认')
  await expect(panel.getByRole('button', { name: '启动 ASR', exact: true })).toBeDisabled()
  state = 'failed'; await panel.getByRole('button', { name: '刷新服务状态' }).click()
  await expect(panel).toContainText('模拟模型加载失败')
  unavailable = true; await panel.getByRole('button', { name: '刷新服务状态' }).click()
  await expect(badge).toHaveText('状态未确认')
  await expect(panel).toContainText('服务状态读取失败')
  await writeFile(path.join(out, 'browser-simulated.json'), JSON.stringify({ kind: 'simulated-management-responses-only', actualModelFault: false, passedAt: new Date().toISOString() }, null, 2), 'utf8')
})

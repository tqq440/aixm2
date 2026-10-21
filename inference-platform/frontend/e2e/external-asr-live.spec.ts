import { expect, test, type Page } from '@playwright/test'
import { createHash } from 'node:crypto'
import { mkdir, readFile, writeFile } from 'node:fs/promises'
import path from 'node:path'
import type { ExternalAsrCapacity, ModelSpec, TaskReceipt, TaskResultView, TaskView } from '../src/types/api'

// Opt-in real-service evidence. No API interception, response fixtures or
// automatic replay: a durable intent prevents an uncertain run creating work.
test.describe.configure({ retries: 0 })
const out = path.resolve(process.cwd(), process.env.E2E_EVIDENCE_DIR ?? '../runtime/verification/asr4b/browser-live')
const audio = path.resolve(process.cwd(), '../runtime/samples/asr_example_zh.wav')
const unknownVersion = { versionBasis: 'integration_revision', upstreamVersion: null } as const

async function readOptionalJson(file: string): Promise<unknown | null> {
  try { return JSON.parse(await readFile(file, 'utf8')) as unknown }
  catch (error) {
    if ((error as NodeJS.ErrnoException).code === 'ENOENT') return null
    throw error
  }
}

async function downloaded(page: Page, label: string, filename: string) {
  const pending = page.waitForEvent('download')
  await page.getByRole('button', { name: label, exact: true }).click()
  const item = await pending
  expect(await item.failure()).toBeNull()
  const destination = path.join(out, filename)
  await item.saveAs(destination)
  return destination
}

test('真实外部 ASR：未知上游版本、一次上传、真实转写与下载、刷新保留', async ({ page, request }) => {
  test.skip(process.env.E2E_REMOTE_ASR_REAL !== '1', '须明确设置 E2E_REMOTE_ASR_REAL=1；使用当前真实外部服务，最多新建一个持久化任务。')
  test.setTimeout(240_000)
  await mkdir(out, { recursive: true })
  const receiptPath = path.join(out, 'submitted-task.json')
  const intentPath = path.join(out, 'submit-intent.json')
  const previous = await readOptionalJson(receiptPath) as { taskId?: string; idempotencyKey?: string; receipt?: TaskReceipt } | null
  if (!previous && await readOptionalJson(intentPath)) {
    throw new Error('此证据目录已有提交意图但无任务回执。请先人工核对原请求；本脚本不会重新上传，也不要通过更换证据目录绕过保护。')
  }
  if (previous && (!previous.taskId || !/^[a-f0-9]{32}$/.test(previous.taskId) || !previous.idempotencyKey)) {
    throw new Error('已有回执信息不完整，须人工核对，不能重新上传。')
  }
  const pageErrors: string[] = []
  const consoleErrors: string[] = []
  page.on('pageerror', error => pageErrors.push(error.message))
  page.on('console', message => { if (message.type() === 'error') consoleErrors.push(message.text()) })
  const report: Record<string, unknown> = { evidenceKind: 'real-browser-real-platform-real-external-asr',
    noNetworkMocking: true, startedAt: new Date().toISOString(), resumedExistingTask: Boolean(previous),
    newTaskSubmissionCount: 0, idempotentReplayTested: false, remoteLifecycleTested: false, passed: false }
  const save = () => writeFile(path.join(out, 'external-asr-live-report.json'), JSON.stringify(report, null, 2), 'utf8')
  let taskId = previous?.taskId ?? ''
  let modelId = ''
  let modelVersion = ''
  try {
    if (process.env.E2E_ASR_CAPACITY_CHECK === '1') {
      const response = await request.get('/api/asr/external/capacity')
      expect(response.status()).toBe(200)
      const capacity = (await response.json()).data as ExternalAsrCapacity
      expect(capacity.registered).toBe(true)
      expect(capacity.deviceKind).toBe('unknown')
      expect(capacity.targetLimit).toBe(1)
      expect(capacity.groupLimit).toBe(1)
      expect(capacity.slots).toHaveLength(1)
      expect(capacity.slots[0].modelReady).toBe(true)
      expect(capacity.slots[0].fresh).toBe(true)
      report.capacityBefore = capacity
    }
    if (!previous) {
      const bytes = await readFile(audio)
      const sha256 = createHash('sha256').update(bytes).digest('hex')
      expect(sha256).toBe('a1bd32dc78493c123f9625a66deee562aed2895f53fbc39f2cca3be7e6f4f20f')
      // Persist the exact key before forwarding native fetch. This observes the
      // original request without changing its body, headers or destination.
      await page.exposeFunction('__recordRemoteAsrSubmission', async (value: Record<string, string>) => {
        if (value.modelId !== modelId || value.modelVersion !== modelVersion || !value.idempotencyKey) {
          throw new Error('实际提交身份与所选登记不一致，未发送请求。')
        }
        await writeFile(path.join(out, 'submission-request.json'), JSON.stringify({ ...value, at: new Date().toISOString() }, null, 2), { encoding: 'utf8', flag: 'wx' })
      })
      await page.addInitScript(() => {
        const original = window.fetch.bind(window)
        const recorded = window as unknown as Window & { __recordRemoteAsrSubmission: (value: Record<string, string>) => Promise<void> }
        window.fetch = async (input, init) => {
          if (typeof input === 'string' && new URL(input, window.location.href).pathname === '/api/tasks/asr'
            && init?.method === 'POST' && init.body instanceof FormData) {
            await recorded.__recordRemoteAsrSubmission({ modelId: String(init.body.get('modelId') ?? ''),
              modelVersion: String(init.body.get('modelVersion') ?? ''), idempotencyKey: new Headers(init.headers).get('Idempotency-Key') ?? '' })
          }
          return original(input, init)
        }
      })
      await page.goto('/')
      const response = await request.get('/api/models')
      expect(response.status()).toBe(200)
      const models = (await response.json()).data.items as ModelSpec[]
      const externalModels = models.filter(model => model.adapterKey === 'remote-v2-sensevoice' && model.versionInfo?.versionBasis === 'integration_revision')
      expect(externalModels).toHaveLength(1)
      const model = externalModels[0]
      expect(model.versionInfo).toEqual(unknownVersion)
      modelId = model.modelId; modelVersion = model.modelVersion
      const external = page.getByRole('region', { name: 'ASR 服务状态', exact: true })
      await expect(external.locator('.asr-service-state')).toHaveText('可以转写')
      await expect(external).toContainText('上游模型版本未知')
      await expect(external.getByRole('button', { name: /启动|停止/ })).toHaveCount(0)
      await page.getByText('已有实例管理与兼容入口', { exact: true }).click()
      await expect(page.getByRole('region', { name: 'ASR 服务管理', exact: true }).locator('.asr-service-state')).toHaveText('未启动')
      await page.getByText('已有实例管理与兼容入口', { exact: true }).click()
      await page.getByLabel('模型', { exact: true }).selectOption(JSON.stringify([modelId, modelVersion]))
      await expect(page.getByRole('region', { name: '新建转写任务', exact: true })).toContainText('上游模型版本未知 · 接入修订')
      await page.getByLabel('选择音频', { exact: true }).setInputFiles(audio)
      await expect(page.getByRole('button', { name: '提交转写', exact: true })).toBeEnabled()
      await writeFile(intentPath, JSON.stringify({ at: new Date().toISOString(), modelId, modelVersion, versionInfo: unknownVersion,
        audio, audioSha256: sha256, automaticRetry: false }, null, 2), { encoding: 'utf8', flag: 'wx' })
      const pending = page.waitForResponse(item => item.request().method() === 'POST' && new URL(item.url()).pathname === '/api/tasks/asr')
      await page.getByRole('button', { name: '提交转写', exact: true }).click()
      const accepted = await pending
      const envelope = await accepted.json()
      const receipt = envelope.data as TaskReceipt | undefined
      const idempotencyKey = accepted.request().headers()['idempotency-key']
      // Save even a malformed/non-success response before any assertion. A
      // later invocation must inspect it, never infer that retrying is safe.
      await writeFile(receiptPath, JSON.stringify({ at: new Date().toISOString(), httpStatus: accepted.status(),
        taskId: receipt?.task?.taskId, idempotencyKey, receipt, error: envelope.error }, null, 2), { encoding: 'utf8', flag: 'wx' })
      expect(accepted.status()).toBe(201)
      expect(receipt?.replayed).toBe(false)
      taskId = receipt!.task.taskId
      expect(taskId).toMatch(/^[a-f0-9]{32}$/)
      expect(receipt!.task.versionInfo).toEqual(unknownVersion)
      report.newTaskSubmissionCount = 1
      report.localAsrStoppedDuringSubmission = true
      report.idempotencyKey = idempotencyKey
    } else {
      await page.goto(`/?task=${taskId}`)
      report.idempotencyKey = previous.idempotencyKey
    }
    report.taskId = taskId
    await save()
    await expect.poll(async () => {
      const response = await request.get(`/api/tasks/${taskId}`)
      expect(response.status()).toBe(200)
      const task = (await response.json()).data as TaskView
      if (['failed', 'cancelled'].includes(task.status)) throw new Error(`真实任务已结束为 ${task.status}；仅保留证据，不重发音频。`)
      return task.status
    }, { timeout: 150_000, intervals: [1000, 2000] }).toBe('succeeded')
    const task = (await (await request.get(`/api/tasks/${taskId}`)).json()).data as TaskView
    const resultResponse = await request.get(`/api/tasks/${taskId}/result`)
    expect(resultResponse.status()).toBe(200)
    const result = (await resultResponse.json()).data as TaskResultView
    expect(task.versionInfo).toEqual(unknownVersion)
    expect(result.versionInfo).toEqual(unknownVersion)
    expect(result.modelVersion).toBe(task.modelVersion)
    expect(result.taskId).toBe(taskId)
    expect(task.attempt).toBe(1)
    expect(task.maxAttempts).toBe(1)
    expect(task.remoteOutcome).toBe('confirmed')
    expect(result.output.text.trim().length).toBeGreaterThan(0)
    expect(result.output.segments.length).toBeGreaterThan(0)
    expect(result.output.engineMetadata?.adapterKey).toBe('remote-v2-sensevoice')
    await page.goto(`/?task=${taskId}`)
    if (process.env.E2E_ASR_CAPACITY_CHECK === '1') {
      const panel = page.getByRole('region', { name: 'ASR 服务状态', exact: true })
      await expect(panel.getByRole('heading', { name: '调用容量', exact: true })).toBeVisible()
      await expect(panel.getByText('调用槽 1', { exact: true })).toBeVisible()
      await expect(panel.getByText('设备：未知', { exact: true })).toBeVisible()
      await expect(panel.getByRole('button', { name: /启动|停止/ })).toHaveCount(0)
      const capacity = (await (await request.get('/api/asr/external/capacity')).json()).data as ExternalAsrCapacity
      expect(capacity.held + capacity.uncertain).toBe(0)
      expect(capacity.remainingCapacity).toBe(1)
      report.capacityAfter = capacity
      report.realServiceConcurrencyRemainsOne = true
    }
    const detail = page.getByRole('region', { name: '任务详情', exact: true })
    await expect(page.getByTestId('transcript-text')).toHaveText(result.output.text)
    await expect(detail).toContainText('上游模型版本未知')
    await page.getByRole('tab', { name: '时间分段', exact: true }).click()
    for (const segment of result.output.segments) await expect(detail).toContainText(segment.text.trim())
    await page.getByRole('tab', { name: '执行信息', exact: true }).click()
    const info = page.getByRole('tabpanel')
    await expect(info).toContainText('上游模型版本')
    await expect(info).toContainText('未知')
    await expect(info).toContainText('接入修订')
    await expect(info).toContainText(result.modelVersion)
    const txt = await downloaded(page, '下载 TXT', 'transcript.txt')
    const json = await downloaded(page, '下载 JSON', 'result.json')
    expect(await readFile(txt, 'utf8')).toBe(result.output.text)
    expect(JSON.parse(await readFile(json, 'utf8'))).toEqual(result)
    await page.reload()
    await expect(page).toHaveURL(new RegExp(`task=${taskId}`))
    await expect(page.getByTestId('transcript-text')).toHaveText(result.output.text)
    const layouts: Record<string, unknown>[] = []
    for (const viewport of [{ width: 1366, height: 900 }, { width: 390, height: 844 }]) {
      await page.setViewportSize(viewport)
      const documentWidth = await page.evaluate(() => document.documentElement.scrollWidth)
      expect(documentWidth).toBeLessThanOrEqual(viewport.width)
      const screenshot = path.join(out, `external-live-${viewport.width}.png`)
      await page.screenshot({ path: screenshot, fullPage: true })
      const referenceBox = await detail.locator('.task-reference').boundingBox()
      const versionBox = await detail.locator('.model-version').boundingBox()
      expect(referenceBox).not.toBeNull()
      expect(versionBox).not.toBeNull()
      expect(versionBox!.y).toBeGreaterThanOrEqual(referenceBox!.y + referenceBox!.height)
      await detail.screenshot({ path: path.join(out, `external-detail-${viewport.width}.png`) })
      layouts.push({ viewport, documentWidth, screenshot })
    }
    expect(pageErrors).toEqual([])
    expect(consoleErrors).toEqual([])
    Object.assign(report, { task, result, layouts, downloadsMatchPersistedResult: true, refreshPreservedTask: true,
      passed: true, completedAt: new Date().toISOString() })
  } catch (error) {
    report.failedAt = new Date().toISOString()
    report.failure = error instanceof Error ? error.message : '真实浏览器验收未完成'
    throw error
  } finally {
    report.taskId = taskId || null
    report.pageErrors = pageErrors
    report.consoleErrors = consoleErrors
    await save()
  }
})

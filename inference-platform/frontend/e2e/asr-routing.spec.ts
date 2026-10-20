import { expect, test, type Page } from '@playwright/test'
import { mkdir, readFile, writeFile } from 'node:fs/promises'
import path from 'node:path'
import type { ModelSpec, TaskResultView, TaskView } from '../src/types/api'
import type { AsrCatalogTarget, AsrSchedulingStatus } from '../src/types/asrScheduling'
import { catalogFixture, resourceFixture, schedulingFixture, targetFixture, taskRoutingFixture } from '../src/test/asrSchedulingFixtures'

// All API requests terminate in these local browser substitutes. No real
// model call, source registration, management operation or daily DB access.
const out = path.resolve(process.cwd(), process.env.E2E_EVIDENCE_DIR ?? '../runtime/verification/laptop-2c/browser')
const taskId = '6'.repeat(32)
const local: ModelSpec = { modelId: 'whisper-small', modelVersion: 'fixture-local-v1', modelRepo: 'fixture/whisper', taskType: 'asr', executionMode: 'offline', adapterKey: 'faster-whisper', parameterSchema: { defaultLanguage: 'zh', autoDetectionValue: 'auto', languages: ['zh'] }, inputConstraints: { maxFileBytes: 20 * 1024 * 1024, maxAudioSeconds: 300 }, outputCapabilities: ['text', 'segments'] }
const remote: ModelSpec = { ...local, modelId: 'fixture-asr', modelVersion: 'fixture-v1', modelRepo: null, adapterKey: 'remote-v2-sensevoice', executionLocation: 'external', managementMode: 'external_call_only' }
const task = (): TaskView => ({ taskId, callerId: 'isolated-browser', taskType: 'asr', modelId: remote.modelId, modelVersion: remote.modelVersion, parameters: { language: null }, status: 'queued', attempt: 0, maxAttempts: 1, executionPlanVersion: 1, input: { fileName: 'isolated-audio.wav', sizeBytes: 12, sha256: '0'.repeat(64), durationSeconds: 1 }, createdAt: new Date().toISOString(), updatedAt: new Date().toISOString(), startedAt: null, finishedAt: null, queueDeadlineAt: null, totalDeadlineAt: null, attemptDeadlineAt: null, nextRetryAt: null, error: null, resultAvailable: false, message: '浏览器隔离任务，不是真实转写。', cancelAllowed: true, cancelUnavailableReason: null, remoteOutcome: 'not_dispatched' })
const result: TaskResultView = { taskId, modelId: remote.modelId, modelVersion: remote.modelVersion, schemaVersion: '1', output: { text: '浏览器隔离结果，不是真实模型输出。', segments: [], language: null, languageRequested: null, languageProbability: null, audioDurationSeconds: 1, engineMetadata: { adapterKey: remote.adapterKey, runtimeVersions: {} } } }

async function fixture(page: Page) {
  const data = {
    status: schedulingFixture() as AsrSchedulingStatus,
    targets: [targetFixture()] as AsrCatalogTarget[],
    directoryFailure: false, statusFailure: false,
    current: null as TaskView | null,
    requests: [] as string[], unexpected: [] as string[], errors: [] as string[],
  }
  page.on('pageerror', error => data.errors.push(error.message))
  await page.route(url => url.pathname.startsWith('/api/'), async route => {
    const url = new URL(route.request().url())
    const call = `${route.request().method()} ${url.pathname}`
    data.requests.push(call)
    const ok = (value: unknown) => route.fulfill({ status: 200, json: { data: value, requestId: 'isolated-routing-browser' } })
    const failed = () => route.fulfill({ status: 503, json: { error: { code: 'FIXTURE_UNAVAILABLE', message: '隔离测试暂时无法读取', details: [] }, requestId: 'isolated-routing-browser' } })
    if (route.request().method() !== 'GET') {
      data.unexpected.push(call)
      return failed()
    }
    const now = new Date().toISOString()
    if (url.pathname === '/api/asr/external/scheduling') return data.statusFailure ? failed() : ok({ ...data.status, observedAt: now })
    if (url.pathname === '/api/asr/external/targets') {
      if (data.directoryFailure) return failed()
      const offset = Number(url.searchParams.get('offset'))
      const limit = Number(url.searchParams.get('limit'))
      return ok(catalogFixture({ observedAt: now, total: data.targets.length, offset, limit, items: data.targets.slice(offset, offset + limit) }))
    }
    if (url.pathname === '/api/asr/external/status') return ok({ configured: true, available: true, state: 'ready', observedAt: now, message: '隔离测试调用状态，不表示真实服务就绪', currentTaskId: null, uncertainTaskId: null, modelId: remote.modelId, modelVersion: remote.modelVersion, capacity: null })
    if (url.pathname === '/api/models') return ok({ items: ['vision', 'llm'].includes(url.searchParams.get('taskType') ?? '') ? [] : [local, remote] })
    if (url.pathname === '/api/runtime') {
      const component = { reportedState: 'ready', availability: 'online', ready: true, modelReady: true, pid: 1, childPid: null, activeTaskId: null, heartbeatAt: now, heartbeatAgeSeconds: 0, lastError: null }
      return ok({ observedAt: now, apiStatus: 'running', worker: { ...component, componentId: 'worker', availability: 'stopped' }, visionWorker: { ...component, componentId: 'vision_worker' }, dispatcher: { ...component, componentId: 'dispatcher' }, executionConcurrency: 1, automaticExecutionRetry: true, message: '隔离状态' })
    }
    if (url.pathname === '/api/nodes' || url.pathname === '/api/instances') return ok({ items: [], total: 0, page: 1, pageSize: 100 })
    if (url.pathname === '/api/tasks') {
      const items = url.searchParams.get('taskType') !== 'vision' && data.current ? [data.current] : []
      return ok({ items, total: items.length, page: 1, pageSize: 20 })
    }
    if (url.pathname === `/api/tasks/${taskId}`) return ok(data.current && { ...data.current, routing: data.current.routing ? { ...data.current.routing, observedAt: now } : undefined })
    if (url.pathname === `/api/tasks/${taskId}/result`) return ok(result)
    if (url.pathname === '/api/llm/status') return ok({ configured: false, availability: 'unconfigured', message: '尚未配置真实大模型服务', maxConcurrentRequests: 1, activeRequests: 0, uncertainPermits: 0, serviceVerified: false })
    if (url.pathname === '/api/llm/requests') return ok({ items: [], total: 0, page: 1, pageSize: 20 })
    data.unexpected.push(call)
    return route.fulfill({ status: 404, json: { error: { code: 'FIXTURE_UNEXPECTED', message: '未定义的隔离接口', details: [] }, requestId: 'isolated-routing-browser' } })
  })
  return data
}

async function record(name: string, data: Awaited<ReturnType<typeof fixture>>, facts: Record<string, boolean>) {
  expect(data.unexpected).toEqual([])
  expect(data.errors).toEqual([])
  expect(data.requests.every(call => call.startsWith('GET '))).toBe(true)
  await mkdir(out, { recursive: true })
  await writeFile(path.join(out, `${name}.json`), JSON.stringify({ evidenceKind: 'isolated-browser-contract-fixtures', realModelInvoked: false, userDatabaseAccessed: false, passedAt: new Date().toISOString(), ...facts, unexpected: data.unexpected, pageErrors: data.errors }, null, 2), 'utf8')
}

test('只读调度目录：固定通道、分页、过期与查询失败不保留可用资源', async ({ page }) => {
  const data = await fixture(page)
  data.targets = Array.from({ length: 6 }, (_, index) => targetFixture({ targetId: `fixture-${index + 1}`, deviceKind: index ? 'gpu' : 'unknown', observations: index ? [] : [resourceFixture()] }))
  await page.goto('/')
  const panel = page.getByRole('region', { name: 'ASR 调度', exact: true })
  await expect(panel.getByText('原固定通道', { exact: true })).toBeVisible()
  await expect(panel.getByText(/本机 Whisper 不是外部模型的 CPU 替代/)).toBeVisible()
  await panel.getByText('目标与资源报告', { exact: true }).click()
  await expect(panel.getByText('2048 MiB', { exact: true })).toBeVisible()
  await expect(panel.getByText('设备待核实', { exact: true })).toBeVisible()
  await expect(panel.getByRole('article')).toHaveCount(5)
  await panel.getByRole('button', { name: '下一页目标', exact: true }).click()
  await expect(panel.getByRole('article')).toHaveCount(1)
  await expect(panel.getByRole('heading', { name: 'fixture-6', exact: true })).toBeVisible()
  await panel.getByRole('button', { name: '上一页目标', exact: true }).click()
  await expect(panel.getByText('2048 MiB', { exact: true })).toBeVisible()
  data.targets[0].observations[0] = resourceFixture({ freshness: 'stale', observedAt: new Date(Date.now() - 120_000).toISOString(), recordedAt: new Date(Date.now() - 119_000).toISOString(), expiresAt: new Date(Date.now() - 60_000).toISOString() })
  await panel.getByRole('button', { name: '刷新调度信息', exact: true }).click()
  await expect(panel.getByText('采样已过期', { exact: true })).toBeVisible()
  await expect(panel.getByText('2048 MiB', { exact: true })).toHaveCount(0)
  data.directoryFailure = true
  await panel.getByRole('button', { name: '刷新调度信息', exact: true }).click()
  await expect(panel.getByText('目标目录读取失败', { exact: true })).toBeVisible()
  await expect(panel.getByText('采样未确认', { exact: true })).toBeVisible()
  await expect(panel.getByText('报告就绪', { exact: true })).toHaveCount(0)
  await mkdir(out, { recursive: true })
  for (const width of [1366, 390]) {
    await page.setViewportSize({ width, height: 900 })
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
    await page.screenshot({ path: path.join(out, `scheduling-directory-${width}.png`), fullPage: true })
  }
  await record('scheduling-directory-isolated', data, { paginationVerified: true, staleMeasurementsSuppressed: true, failedDirectoryUnconfirmed: true, responsiveLayout: true })
})

test('任务调度：等待原因、已选目标和未知结果不重新推荐或重发', async ({ page }) => {
  const data = await fixture(page)
  data.status = schedulingFixture({ mode: 'isolated', executionScope: 'isolated', routingConfigured: true, candidateCount: 2, message: '当前仅启用隔离候选。' })
  data.current = { ...task(), routing: taskRoutingFixture() }
  await page.goto(`/?task=${taskId}`)
  const panel = page.getByRole('region', { name: '任务调度说明', exact: true })
  await expect(page.getByText('隔离调度验证', { exact: true })).toBeVisible()
  await expect(panel.getByRole('heading', { name: '等待选择执行目标', exact: true })).toBeVisible()
  await panel.getByText('查看候选等待原因', { exact: true }).click()
  await expect(panel.getByText('资源报告已过期，请核对新的观测。', { exact: true })).toBeVisible()
  data.current = { ...data.current, status: 'running', attempt: 1, cancelAllowed: false, remoteOutcome: 'pending', routing: taskRoutingFixture({ selectionState: 'selected', catalogRevisionId: '5'.repeat(64), targetId: 'fixture-gpu', deviceKind: 'gpu', reasonCode: 'GPU_PREFERRED', reasonCategory: 'selected', selectedAt: new Date().toISOString(), message: '已按 GPU 优先规则选择目标。', candidates: [] }) }
  await page.getByRole('button', { name: '刷新任务', exact: true }).click()
  await expect(panel.getByRole('heading', { name: '已选择 GPU 目标', exact: true })).toBeVisible()
  await expect(page.getByRole('button', { name: '取消任务', exact: true })).toBeDisabled()
  data.current = { ...data.current, status: 'failed', remoteOutcome: 'unknown', routing: { ...data.current.routing!, statusCode: 'REMOTE_ASR_RESULT_UNKNOWN', reasonCategory: 'outcome_unknown', message: '远端结果待核对。', candidates: [{ targetId: 'fixture-cpu', deviceKind: 'cpu', eligible: true, reasonCode: 'CPU_FALLBACK', message: '这个候选不得重新推荐给已选择任务。' }] } }
  await page.getByRole('button', { name: '刷新任务', exact: true }).click()
  await expect(panel.getByRole('heading', { name: '原目标结果待核对', exact: true })).toBeVisible()
  await expect(panel.getByText(/不会自动转到 CPU 或其他目标重发/)).toBeVisible()
  await expect(panel.getByText(/是否仍占用许可，请核对调用记录与当前调用容量/)).toBeVisible()
  await expect(panel.getByText('fixture-gpu', { exact: true })).toBeVisible()
  await expect(panel.getByText('fixture-cpu', { exact: true })).toHaveCount(0)
  await expect(panel.getByText('查看候选等待原因', { exact: true })).toHaveCount(0)
  await expect(panel.getByText('4'.repeat(64), { exact: false })).toHaveCount(0)
  await mkdir(out, { recursive: true })
  await page.screenshot({ path: path.join(out, 'scheduling-unknown-target.png'), fullPage: true })
  await record('scheduling-task-isolated', data, { waitingReasonVerified: true, frozenSelectionPreserved: true, unknownOutcomeNotResent: true, noNewCandidateRecommendation: true })
})

test('旧结果与工作台兼容：外部旧任务下载，Vision 和 LLM 入口保留', async ({ page }) => {
  const data = await fixture(page)
  data.current = { ...task(), status: 'succeeded', resultAvailable: true, remoteOutcome: 'confirmed', cancelAllowed: false }
  await page.goto(`/?task=${taskId}`)
  await expect(page.getByText(/原固定通道任务：未记录 GPU \/ CPU 选择/)).toBeVisible()
  await expect(page.getByTestId('transcript-text')).toHaveText(result.output.text)
  const downloadEvent = page.waitForEvent('download')
  await page.getByRole('button', { name: '下载 JSON', exact: true }).click()
  const download = await downloadEvent
  expect(JSON.parse(await readFile((await download.path())!, 'utf8'))).toEqual(result)
  await page.getByRole('button', { name: '图片检测', exact: true }).click()
  await expect(page.getByRole('heading', { name: /看见图片中的目标/ })).toBeVisible()
  await expect(page.getByRole('region', { name: 'ASR 调度', exact: true })).toBeHidden()
  await page.getByRole('button', { name: '文本生成', exact: true }).click()
  await expect(page.getByRole('heading', { name: /与文字，实时连接/ })).toBeVisible()
  await expect(page.getByRole('button', { name: '开始生成', exact: true })).toBeDisabled()
  await page.getByRole('button', { name: '音频转写', exact: true }).click()
  await expect(page.getByTestId('transcript-text')).toHaveText(result.output.text)
  await record('scheduling-compatibility-isolated', data, { legacyResultDownload: true, visionEntryPreserved: true, llmEntryPreserved: true, noInferenceSubmitted: true })
})

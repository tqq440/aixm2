import { expect, test, type Page } from '@playwright/test'
import { mkdir, readFile, writeFile } from 'node:fs/promises'
import path from 'node:path'
import type { ExternalAsrStatus, ModelSpec, TaskResultView, TaskView } from '../src/types/api'
import { capacityFixture } from '../src/test/asrCapacityFixtures'
import { catalogFixture, schedulingFixture } from '../src/test/asrSchedulingFixtures'
import { serviceFixture, servicesFixture } from '../src/test/asrServicesFixtures'
import type { AsrServiceConfig } from '../src/types/asrServices'

// Every /api request is intercepted. This is browser contract evidence only;
// it never connects to a model, registers a daily model or writes the user DB.
const out = path.resolve(process.cwd(), process.env.E2E_EVIDENCE_DIR ?? '../runtime/verification/asr4a/frontend')
const local: ModelSpec = { modelId: 'whisper-small', modelVersion: 'fixture-local-v1', modelRepo: 'fixture/whisper', taskType: 'asr', executionMode: 'offline', adapterKey: 'faster-whisper', parameterSchema: { defaultLanguage: 'zh', autoDetectionValue: 'auto', languages: ['zh'] }, inputConstraints: { maxFileBytes: 20 * 1024 * 1024, maxAudioSeconds: 300 }, outputCapabilities: ['text', 'segments'] }
const remote: ModelSpec = { ...local, modelId: 'sensevoice-fixture', modelVersion: 'fixture-remote-v1', modelRepo: null, adapterKey: 'remote-v2-sensevoice', executionLocation: 'external', managementMode: 'external_call_only' }
const taskId = '4'.repeat(32)
const sample: TaskView = { taskId, callerId: 'browser-fixture', taskType: 'asr', modelId: remote.modelId, modelVersion: remote.modelVersion, parameters: { language: null }, status: 'queued', attempt: 0, maxAttempts: 1, executionPlanVersion: 1, input: { fileName: 'fixture-audio.wav', sizeBytes: 12, sha256: '0'.repeat(64), durationSeconds: 1 }, createdAt: new Date().toISOString(), updatedAt: new Date().toISOString(), startedAt: null, finishedAt: null, queueDeadlineAt: null, totalDeadlineAt: null, attemptDeadlineAt: null, nextRetryAt: null, error: null, resultAvailable: false, message: '隔离浏览器测试任务', cancelAllowed: true, cancelUnavailableReason: null, remoteOutcome: 'not_dispatched' }
const result: TaskResultView = { taskId, modelId: remote.modelId, modelVersion: remote.modelVersion, schemaVersion: '1', output: { text: '这是隔离测试文字，不是真实模型转写。', segments: [{ start: 0, end: 1, text: '这是隔离测试文字，不是真实模型转写。' }], language: null, languageRequested: null, languageProbability: null, audioDurationSeconds: 1, engineMetadata: { adapterKey: remote.adapterKey, runtimeVersions: { 'remote-api': '14.2' } } } }

async function fixture(page: Page) {
  const data = { configured: true, state: 'ready' as ExternalAsrStatus['state'], available: true, disconnect: false, current: null as TaskView | null, models: [local, remote], localReady: false, submissions: [] as string[], cancelled: 0, unexpected: [] as string[], capacity: null as ExternalAsrStatus['capacity'],
    services: servicesFixture({ runningModel: { modelId: remote.modelId, modelVersion: remote.modelVersion }, items: [serviceFixture({ modelId: remote.modelId })] }), healthTests: 0, writes: 0 }
  await page.route(url => url.pathname.startsWith('/api/'), async route => {
    const url = new URL(route.request().url())
    const ok = (value: unknown, status = 200) => route.fulfill({ status, json: { data: value, requestId: 'isolated-browser-fixture' } })
    if (url.pathname === '/api/asr/services' && route.request().method() === 'GET') return ok(data.services)
    if (url.pathname === '/api/asr/services/test' && route.request().method() === 'POST') {
      data.healthTests += 1
      return ok({ success: true, phase: 'health', message: '隔离接口健康检查通过', durationMs: 12, configRevision: 'b'.repeat(64), errorCode: null, apiVersion: 'fixture' })
    }
    if (url.pathname === '/api/asr/services/selection' && route.request().method() === 'POST') {
      const body = route.request().postDataJSON() as { serviceId: string | null; expectedRevision: string | null }
      data.services = { ...data.services, selectedServiceId: body.serviceId, selectedRevision: body.expectedRevision, restartRequired: true }
      return ok(data.services)
    }
    if (url.pathname.startsWith('/api/asr/services/') && route.request().method() === 'PUT') {
      const body = route.request().postDataJSON() as { config: AsrServiceConfig }
      data.writes += 1
      data.services = { ...data.services, items: [...data.services.items.filter(item => item.serviceId !== body.config.serviceId), { ...body.config, revision: 'b'.repeat(64) }] }
      return ok(data.services)
    }
    if (route.request().method() === 'GET' && url.pathname === '/api/asr/external/scheduling') return ok(schedulingFixture())
    if (route.request().method() === 'GET' && url.pathname === '/api/asr/external/targets') return ok(catalogFixture({
      total: 0, items: [], offset: Number(url.searchParams.get('offset')), limit: Number(url.searchParams.get('limit')),
    }))
    if (url.pathname === '/api/asr/external/status') {
      if (data.disconnect) return route.abort()
      return ok({ configured: data.configured, available: data.available, state: data.state, observedAt: new Date().toISOString(), message: data.state === 'uncertain' ? '远端请求结果待核对' : '隔离测试通道状态', currentTaskId: data.state === 'running' ? taskId : null, uncertainTaskId: data.state === 'uncertain' ? taskId : null, modelId: data.configured ? remote.modelId : null, modelVersion: data.configured ? remote.modelVersion : null, capacity: data.capacity })
    }
    if (url.pathname === '/api/models') return ok({ items: url.searchParams.get('taskType') === 'vision' ? [] : data.models })
    if (url.pathname === '/api/runtime') {
      const component = { reportedState: 'ready', availability: 'online', ready: true, modelReady: true, pid: 1, childPid: null, activeTaskId: null, heartbeatAt: new Date().toISOString(), heartbeatAgeSeconds: 0, lastError: null }
      return ok({ observedAt: new Date().toISOString(), apiStatus: 'running', worker: { ...component, componentId: 'worker', availability: data.localReady ? 'online' : 'stopped' }, remoteAsrWorker: { ...component, componentId: 'remote_asr_worker', availability: data.configured ? 'online' : 'not_started' }, visionWorker: { ...component, componentId: 'vision_worker' }, dispatcher: { ...component, componentId: 'dispatcher' }, executionConcurrency: 1, automaticExecutionRetry: true, message: 'fixture' })
    }
    if (url.pathname === '/api/nodes') return ok({ items: [{ nodeId: 'a'.repeat(32), hostname: '隔离测试节点', osName: 'Windows', osVersion: 'fixture', architecture: 'AMD64', cpuCount: 1, gpuStatus: 'not_probed', registrationState: 'registered', agentStatus: 'online', registeredAt: new Date().toISOString(), metadataObservedAt: new Date().toISOString() }], total: 1, page: 1, pageSize: 100 })
    if (url.pathname === '/api/instances') return ok({ items: [{ instanceId: 'b'.repeat(32), nodeId: 'a'.repeat(32), instanceKey: 'asr-default', modelId: local.modelId, modelVersion: local.modelVersion, adapterKey: local.adapterKey, executionDevice: 'cpu', registrationState: 'registered', runtimeState: 'unknown', managementMode: 'node_agent', registeredAt: new Date().toISOString() }], total: 1, page: 1, pageSize: 100 })
    if (url.pathname === `/api/instances/${'b'.repeat(32)}/management`) return ok({ instanceId: 'b'.repeat(32), nodeId: 'a'.repeat(32), managementMode: 'node_agent', state: data.localReady ? 'ready' : 'stopped', reportedState: data.localReady ? 'ready' : 'stopped', desiredState: data.localReady ? 'running' : 'stopped', agentAvailable: true, agentSessionId: 'c'.repeat(32), agentPid: 1, workerSessionId: null, childPid: null, commandRevision: 0, modelReady: data.localReady, acceptingTasks: data.localReady, activeTaskId: null, lastError: null, heartbeatAt: new Date().toISOString(), evaluatedAt: new Date().toISOString(), exitConfirmedAt: new Date().toISOString(), shutdownConfirmedAt: null })
    if (url.pathname === '/api/tasks/asr') {
      data.submissions.push(route.request().postData() ?? '')
      data.current = { ...sample }
      return ok({ task: data.current, replayed: false }, 201)
    }
    if (url.pathname === '/api/tasks') return ok({ items: data.current ? [data.current] : [], total: data.current ? 1 : 0, page: 1, pageSize: 20 })
    if (url.pathname === `/api/tasks/${taskId}`) return ok(data.current)
    if (url.pathname === `/api/tasks/${taskId}/result`) return ok(result)
    if (url.pathname === `/api/tasks/${taskId}/cancel`) {
      data.cancelled += 1
      data.current = { ...data.current!, status: 'cancelled', cancelAllowed: false, finishedAt: new Date().toISOString() }
      return ok({ task: data.current, decision: 'cancelled' })
    }
    data.unexpected.push(`${route.request().method()} ${url.pathname}`)
    return route.fulfill({ status: 404, json: { error: { code: 'FIXTURE_UNEXPECTED', message: '隔离测试没有定义此接口', details: [] }, requestId: 'fixture' } })
  })
  return data
}

test('隔离浏览器：外部上传、排队取消、执行中禁取消、结果下载与未知结果提示', async ({ page }) => {
  await mkdir(out, { recursive: true })
  const data = await fixture(page)
  const errors: string[] = []
  page.on('pageerror', error => errors.push(error.message))
  await page.goto('/')
  const external = page.getByRole('region', { name: 'ASR 服务状态', exact: true })
  await expect(external.locator('.asr-service-state')).toHaveText('可以转写')
  await expect(external.getByRole('button', { name: /启动|停止/ })).toHaveCount(0)
  await page.getByText('已有实例管理与兼容入口', { exact: true }).click()
  await expect(page.getByRole('region', { name: 'ASR 服务管理', exact: true }).locator('.asr-service-state')).toHaveText('未启动')
  await page.getByText('已有实例管理与兼容入口', { exact: true }).click()
  await page.getByLabel('模型', { exact: true }).selectOption(JSON.stringify([remote.modelId, remote.modelVersion]))
  await expect(page.getByText(/音频会发送到当前运行配置的 ASR 接口/)).toBeVisible()
  await expect(page.getByLabel('识别语言', { exact: true })).toHaveCount(0)
  await page.getByLabel('选择音频', { exact: true }).setInputFiles({ name: 'fixture-audio.wav', mimeType: 'audio/wav', buffer: Buffer.from('fixture-only') })
  await page.getByRole('button', { name: '提交转写', exact: true }).click()
  await expect(page.getByRole('button', { name: '取消任务', exact: true })).toBeEnabled()
  expect(data.submissions).toHaveLength(1)
  expect(data.submissions[0]).not.toContain('name="language"')
  await page.getByRole('button', { name: '取消任务', exact: true }).click()
  await expect(page.getByRole('heading', { name: '任务已取消', exact: true })).toBeVisible()
  expect(data.cancelled).toBe(1)
  await page.getByRole('button', { name: '提交新任务', exact: true }).click()
  await page.getByLabel('选择音频', { exact: true }).setInputFiles({ name: 'fixture-audio.wav', mimeType: 'audio/wav', buffer: Buffer.from('fixture-only') })
  await page.getByRole('button', { name: '提交转写', exact: true }).click()
  data.current = { ...sample, status: 'running', attempt: 1, cancelAllowed: false, cancelUnavailableReason: '远端调用开始后不支持取消', remoteOutcome: 'pending' }
  data.state = 'running'
  await page.getByRole('button', { name: '刷新任务', exact: true }).click()
  await expect(page.getByRole('button', { name: '取消任务', exact: true })).toBeDisabled()
  await expect(page.getByText('远端调用开始后不支持取消', { exact: true })).toBeVisible()
  data.current = { ...data.current, status: 'succeeded', resultAvailable: true, remoteOutcome: 'confirmed', finishedAt: new Date().toISOString() }
  data.state = 'ready'
  await page.getByRole('button', { name: '刷新任务', exact: true }).click()
  await expect(page.getByTestId('transcript-text')).toHaveText(result.output.text)
  const downloadEvent = page.waitForEvent('download')
  await page.getByRole('button', { name: '下载 TXT', exact: true }).click()
  const download = await downloadEvent
  expect(await readFile((await download.path())!, 'utf8')).toBe(result.output.text)
  await page.getByRole('tab', { name: '执行信息', exact: true }).click()
  await expect(page.getByRole('tabpanel')).toContainText('未报告 / 未报告')
  data.current = { ...data.current, status: 'failed', resultAvailable: false, remoteOutcome: 'unknown' }
  data.state = 'uncertain'; data.available = false
  await page.reload()
  await expect(external.locator('.asr-service-state')).toHaveText('远端结果未知')
  await expect(page.getByRole('heading', { name: '远端结果未知', exact: true })).toBeVisible()
  await page.getByLabel('模型', { exact: true }).selectOption(JSON.stringify([remote.modelId, remote.modelVersion]))
  await page.getByLabel('选择音频', { exact: true }).setInputFiles({ name: 'fixture-audio.wav', mimeType: 'audio/wav', buffer: Buffer.from('fixture-only') })
  await expect(page.getByRole('button', { name: '提交转写', exact: true })).toBeDisabled()
  for (const width of [1366, 390]) {
    await page.setViewportSize({ width, height: 900 })
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
    await page.screenshot({ path: path.join(out, `external-fixture-${width}.png`), fullPage: true })
  }
  expect(data.unexpected).toEqual([])
  expect(errors).toEqual([])
  await writeFile(path.join(out, 'external-browser-isolated.json'), JSON.stringify({ evidenceKind: 'isolated-browser-contract-fixtures', realModelInvoked: false, userDatabaseAccessed: false, passedAt: new Date().toISOString(), submissions: data.submissions.length, queuedCancellation: true, runningCancellationDisabled: true, downloadVerified: true, unknownOutcomeBlocked: true, pageErrors: errors }, null, 2), 'utf8')
})

test('隔离浏览器：未配置不制造模型，断线不影响本机提交', async ({ page }) => {
  const data = await fixture(page)
  data.configured = false; data.available = false; data.state = 'unconfigured'; data.models = [local]; data.localReady = true
  await page.goto('/')
  const panel = page.getByRole('region', { name: 'ASR 服务状态', exact: true })
  await expect(panel.locator('.asr-service-state')).toHaveText('尚未配置')
  await expect(page.getByRole('option', { name: /sensevoice/ })).toHaveCount(0)
  await page.getByText('已有实例管理与兼容入口', { exact: true }).click()
  await page.getByRole('button', { name: '使用已有 Whisper 实例提交', exact: true }).click()
  await page.getByLabel('选择音频', { exact: true }).setInputFiles({ name: 'fixture-audio.wav', mimeType: 'audio/wav', buffer: Buffer.from('fixture-only') })
  await expect(page.getByRole('button', { name: '提交转写', exact: true })).toBeEnabled()
  data.disconnect = true
  await panel.getByRole('button', { name: '刷新调用状态', exact: true }).click()
  await expect(panel.locator('.asr-service-state')).toHaveText('状态未确认')
  await expect(page.getByRole('button', { name: '提交转写', exact: true })).toBeEnabled()
  expect(data.unexpected).toEqual([])
})

test('隔离浏览器：多槽容量、共享未知占位与断线，刷新不重发任务', async ({ page }) => {
  await mkdir(out, { recursive: true })
  const data = await fixture(page)
  data.capacity = capacityFixture()
  const errors: string[] = []
  page.on('pageerror', error => errors.push(error.message))
  await page.goto('/')
  const panel = page.getByRole('region', { name: 'ASR 服务状态', exact: true })
  await expect(panel.getByRole('heading', { name: '调用容量', exact: true })).toBeVisible()
  await expect(panel.getByText('2 / 2', { exact: true })).toHaveCount(2)
  await expect(panel.getByText('调用槽 2', { exact: true })).toBeVisible()
  await expect(panel.getByText('设备：未知', { exact: true })).toBeVisible()
  await expect(panel.getByText(/不会自动重发到其他槽或设备/)).toBeVisible()
  for (const width of [1366, 390]) {
    await page.setViewportSize({ width, height: 900 })
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
    await page.screenshot({ path: path.join(out, `capacity-isolated-${width}.png`), fullPage: true })
  }
  data.disconnect = true
  await panel.getByRole('button', { name: '刷新调用状态', exact: true }).click()
  await expect(panel.getByText(/容量状态未确认/)).toBeVisible()
  await expect(panel.getByText('2 / 2', { exact: true })).toHaveCount(0)
  data.disconnect = false
  data.capacity.observedAt = new Date().toISOString()
  data.capacity.slots = data.capacity.slots.map(slot => ({ ...slot, state: 'stopping' }))
  data.state = 'stopping'; data.available = false
  await page.reload()
  await expect(panel.locator('.asr-service-state')).toHaveText('停止接单中')
  await expect(panel.getByRole('button', { name: /启动|停止|重试/ })).toHaveCount(0)
  expect(data.submissions).toHaveLength(0)
  expect(data.unexpected).toEqual([])
  expect(errors).toEqual([])
  await writeFile(path.join(out, 'capacity-browser-isolated.json'), JSON.stringify({ evidenceKind: 'isolated-browser-contract-fixtures', realModelInvoked: false, userDatabaseAccessed: false, passedAt: new Date().toISOString(), multipleSlotsVisible: true, sharedUnknownOccupancyVisible: true, staleCountsSuppressed: true, refreshDidNotSubmit: true }, null, 2))
})

test('隔离浏览器：手工配置、连接测试、显式选用与重启后正常音频任务', async ({ page }) => {
  await mkdir(out, { recursive: true })
  const data = await fixture(page)
  const errors: string[] = []
  page.on('pageerror', error => errors.push(error.message))
  await page.goto('/')
  const config = page.getByRole('region', { name: 'ASR 服务配置', exact: true })
  await expect(config.getByRole('heading', { name: 'ASR 服务配置', exact: true })).toBeVisible()
  expect(data.healthTests).toBe(0)
  await config.getByLabel('服务标识', { exact: true }).fill('manual-service')
  await config.getByLabel('服务名称', { exact: true }).fill('手动接口配置')
  await config.getByLabel('服务地址', { exact: true }).fill('http://127.0.0.1:8013')
  await config.getByLabel('平台模型标识', { exact: true }).fill(remote.modelId)
  await config.getByRole('button', { name: '测试连接', exact: true }).click()
  await expect(config.getByText('连接测试通过', { exact: true })).toBeVisible()
  await expect(config.getByText(/尚未验证音频转写/)).toBeVisible()
  expect(data.healthTests).toBe(1)
  expect(data.writes).toBe(0)
  expect(data.submissions).toHaveLength(0)
  await config.getByLabel('服务名称', { exact: true }).fill('手动接口配置二')
  await expect(config.getByText('连接测试通过', { exact: true })).toHaveCount(0)
  await config.getByRole('button', { name: '保存配置', exact: true }).click()
  await expect(config.getByText(/配置已保存/)).toBeVisible()
  expect(data.services.selectedServiceId).toBe('speech-main')
  expect(data.services.restartRequired).toBe(false)
  await config.getByRole('button', { name: '选用此修订', exact: true }).click()
  await expect(config.getByText('待重启生效', { exact: true })).toBeVisible()
  await expect(config.getByRole('button', { name: '用当前服务测试音频', exact: true })).toBeDisabled()
  await page.getByLabel('选择音频', { exact: true }).setInputFiles({ name: 'fixture-audio.wav', mimeType: 'audio/wav', buffer: Buffer.from('fixture-only') })
  await expect(page.getByRole('button', { name: '提交转写', exact: true })).toBeDisabled()
  expect(data.submissions).toHaveLength(0)
  for (const width of [1366, 390]) {
    await page.setViewportSize({ width, height: 900 })
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
    await page.screenshot({ path: path.join(out, `asr-configuration-${width}.png`), fullPage: true })
  }
  // Simulate the backend's next process loading the selected immutable snapshot.
  data.services = { ...data.services, runningServiceId: data.services.selectedServiceId, runningRevision: data.services.selectedRevision, restartRequired: false }
  await page.reload()
  await config.getByRole('button', { name: '用当前服务测试音频', exact: true }).click()
  expect(data.submissions).toHaveLength(0)
  await page.getByLabel('选择音频', { exact: true }).setInputFiles({ name: 'fixture-audio.wav', mimeType: 'audio/wav', buffer: Buffer.from('fixture-only') })
  await page.getByRole('button', { name: '提交转写', exact: true }).click()
  expect(data.submissions).toHaveLength(1)
  data.current = { ...sample, status: 'succeeded', resultAvailable: true, attempt: 1, remoteOutcome: 'confirmed', cancelAllowed: false, finishedAt: new Date().toISOString() }
  await page.getByRole('button', { name: '刷新任务', exact: true }).click()
  await expect(page.getByTestId('transcript-text')).toHaveText(result.output.text)
  expect(data.unexpected).toEqual([])
  expect(errors).toEqual([])
  await writeFile(path.join(out, 'service-configuration-browser-isolated.json'), JSON.stringify({
    evidenceKind: 'isolated-browser-contract-fixtures', realModelInvoked: false, userDatabaseAccessed: false,
    passedAt: new Date().toISOString(), healthTests: data.healthTests, savedConfigurations: data.writes,
    pendingRestartBlocked: true, sampleUsedNormalTaskEndpoint: true, actualModelTested: false,
    newTasks: data.submissions.length, pageErrors: errors,
  }, null, 2))
})

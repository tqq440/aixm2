import { expect, test, type APIResponse, type Page } from '@playwright/test'
import { execFileSync } from 'node:child_process'
import { mkdir, readFile, writeFile } from 'node:fs/promises'
import path from 'node:path'
import type { TaskReceipt, TaskResultView, TaskView, VisionTaskView, DetectionResult } from '../src/types/api'
import type { ManagementStatus } from '../src/types/management'

// Management requests contain a local secret. This suite deliberately disables
// traces, video and automatic screenshots, including on failure.
test.use({ trace: 'off', video: 'off', screenshot: 'off' })
const base = path.resolve(process.cwd(), '..')
const out = path.resolve(process.cwd(), process.env.E2E_EVIDENCE_DIR ?? '../runtime/verification/asr3b')
const audio = path.join(base, 'runtime/samples/asr_example_zh.wav')
const longAudio = path.resolve(base, process.env.E2E_LONG_AUDIO ?? 'runtime/verification/asr2b/repeated-public-sample.wav')

async function downloaded(page: Page, label: string, filename: string) {
  const pending = page.waitForEvent('download')
  await page.getByRole('button', { name: label, exact: true }).click()
  const item = await pending
  expect(await item.failure()).toBeNull()
  const destination = path.join(out, filename)
  await item.saveAs(destination)
  return destination
}

test('真实ASR完整交付：页面启停与再启动、真实结果、在途排空、队列期限及 Vision 独立运行', async ({ page, request }) => {
  test.skip(process.env.E2E_ASR_DELIVERY_REAL !== '1', '明确启用真实闭环验收才运行；会创建三个 ASR 任务及一个 Vision 任务。')
  test.setTimeout(600_000)
  await mkdir(out, { recursive: true })
  // Only capture the configuration value in this process's memory. Do not put
  // it into attachments, assertion values, URLs, console logs or evidence JSON.
  let token: string
  try {
    token = execFileSync(process.env.E2E_PYTHON ?? 'C:/Users/86199/miniconda3/envs/project311/python.exe', [
      '-c', 'from backend.config.settings.app_settings import load_settings; print(load_settings().agent_token.get_secret_value())',
    ], { cwd: base, encoding: 'utf8', windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'] }).trim()
  } catch { throw new Error('无法通过后台配置取得本机管理令牌；错误输出未写入报告。') }
  if (token.length < 32 || token.length > 256) throw new Error('本机管理令牌配置长度不可用')

  const events: Record<string, unknown>[] = []
  const tasks: Record<string, TaskView | VisionTaskView> = {}
  const report: Record<string, unknown> = {
    evidenceKind: 'real-browser-real-agent-real-whisper-real-yolo',
    noNetworkMocking: true, startedAt: new Date().toISOString(), passed: false,
    description: '真实页面完成三次独立 ASR 启动；模型故障测试另行记录，不混入本报告。',
    tasks, events,
  }
  const save = () => writeFile(path.join(out, 'browser-delivery-real.json'), JSON.stringify(report, null, 2), 'utf8')
  const record = async (step: string, evidence: unknown) => {
    events.push({ step, at: new Date().toISOString(), evidence }); await save()
  }
  const pageErrors: string[] = []
  page.on('pageerror', error => pageErrors.push(error.message.split(token).join('[REDACTED]')))
  let instanceId = ''
  const panel = page.getByRole('region', { name: 'ASR 服务管理', exact: true })
  const badge = panel.locator('.asr-service-state')
  const status = async (): Promise<ManagementStatus> => {
    const response = await request.get(`/api/instances/${instanceId}/management`)
    expect(response.status()).toBe(200)
    return (await response.json()).data
  }
  const task = async (taskId: string): Promise<TaskView> => {
    const response = await request.get(`/api/tasks/${taskId}`)
    expect(response.status()).toBe(200)
    return (await response.json()).data
  }
  const sessions = async () => {
    const response = await request.get(`/api/instances/${instanceId}/management/sessions?page=1&pageSize=100`)
    expect(response.status()).toBe(200)
    return (await response.json()).data as { total: number; items: { workerSessionId: string; agentSessionId: string; state: string; exitConfirmedAt: string | null }[] }
  }
  const enterToken = async () => {
    try { await panel.getByLabel('本机管理令牌', { exact: true }).fill(token) }
    catch { throw new Error('页面管理令牌输入未完成；输入值已从错误报告中省略。') }
  }
  const command = async (action: 'start' | 'stop', body?: { agentSessionId: string; expectedRevision: number }): Promise<APIResponse> => {
    const current = await status()
    try {
      return await request.post(`/api/instances/${instanceId}/management/${action}`, {
        headers: { 'X-Management-Token': token },
        data: body ?? { agentSessionId: current.agentSessionId, expectedRevision: current.commandRevision },
      })
    } catch { throw new Error('真实管理请求网络失败；含管理凭据的请求信息未写入报告。') }
  }
  const clickCommand = async (action: 'start' | 'stop') => {
    await enterToken()
    const pending = page.waitForResponse(response => response.request().method() === 'POST'
      && new URL(response.url()).pathname === `/api/instances/${instanceId}/management/${action}`)
    await panel.getByRole('button', { name: action === 'start' ? '启动 ASR' : '停止 ASR', exact: true }).click()
    expect((await pending).status()).toBe(202)
    await record(action === 'start' ? '页面启动请求已受理（尚非就绪）' : '页面停止请求已受理（尚非退出确认）', await status())
  }
  const submitAudio = async (filePath: string, label: string) => {
    await page.getByLabel('选择音频', { exact: true }).setInputFiles(filePath)
    const accepted = page.waitForResponse(response => response.request().method() === 'POST'
      && new URL(response.url()).pathname === '/api/tasks/asr')
    await page.getByRole('button', { name: '提交转写', exact: true }).click()
    const response = await accepted
    expect(response.status()).toBe(201)
    const receipt = (await response.json()).data as TaskReceipt
    expect(receipt.replayed).toBe(false)
    tasks[label] = receipt.task
    await record(`真实音频已提交：${label}`, receipt)
    return receipt.task.taskId
  }
  const completed = async (taskId: string, label: string) => {
    await expect.poll(async () => (await task(taskId)).status, { timeout: 150_000, intervals: [500, 1000, 2000] }).toBe('succeeded')
    const finalTask = await task(taskId)
    const response = await request.get(`/api/tasks/${taskId}/result`)
    expect(response.status()).toBe(200)
    const result = (await response.json()).data as TaskResultView
    expect(finalTask.attempt).toBe(1)
    expect(result.taskId).toBe(taskId)
    expect(result.output.text.trim().length).toBeGreaterThan(0)
    expect(result.output.segments.length).toBeGreaterThan(0)
    expect(result.output.engineMetadata?.device ?? result.output.device).toBe('cpu')
    tasks[label] = finalTask
    await record(`真实转写完成：${label}`, { task: finalTask, result })
    return result
  }

  try {
    await page.goto('/')
    await expect(badge).toHaveText('未启动')
    const nodeId = await panel.getByLabel('计算节点').inputValue()
    expect(nodeId).toMatch(/^[a-f0-9]{32}$/)
    await panel.getByLabel('计算节点').selectOption(nodeId)
    instanceId = await panel.getByLabel('ASR 实例', { exact: true }).inputValue()
    expect(instanceId).toMatch(/^[a-f0-9]{32}$/)
    await panel.getByLabel('ASR 实例', { exact: true }).selectOption(instanceId)
    const initial = await status()
    expect(initial.state).toBe('stopped')
    expect(initial.agentAvailable).toBe(true)
    const initialSessions = await sessions()
    await record('真实本机节点和实例选择，默认 ASR 停止', { nodeId, instanceId, initial, sessions: initialSessions })

    await clickCommand('start')
    await expect(badge).toHaveText('可以转写', { timeout: 120_000 })
    const firstReady = await status()
    expect(firstReady.modelReady).toBe(true)
    expect(firstReady.acceptingTasks).toBe(true)
    expect((await sessions()).total).toBe(initialSessions.total + 1)
    const duplicateStart = await command('start')
    expect(duplicateStart.status()).toBe(202)
    const afterDuplicateStart = await status()
    expect(afterDuplicateStart.workerSessionId).toBe(firstReady.workerSessionId)
    expect(afterDuplicateStart.commandRevision).toBe(firstReady.commandRevision)
    expect((await sessions()).total).toBe(initialSessions.total + 1)
    // Reuse an actual earlier platform session, not a fabricated identity.
    const earlierAgentSessionId = initialSessions.items.find(item => item.agentSessionId !== firstReady.agentSessionId)?.agentSessionId
    expect(earlierAgentSessionId).toMatch(/^[a-f0-9]{32}$/)
    const stale = await command('stop', { agentSessionId: earlierAgentSessionId!, expectedRevision: firstReady.commandRevision })
    expect(stale.status()).toBe(409)
    expect((await stale.json()).error.code).toBe('AGENT_COMMAND_STALE')
    expect((await status()).commandRevision).toBe(firstReady.commandRevision)
    await record('首次模型就绪、重复启动无额外实例、实际历史会话命令拒绝', { firstReady, afterDuplicateStart, earlierAgentSessionId, staleCommandHttpStatus: 409 })

    const shortId = await submitAudio(audio, '短中文')
    const shortResult = await completed(shortId, '短中文')
    const detail = page.getByRole('region', { name: '任务详情', exact: true })
    await expect(detail).toContainText(shortResult.output.text)
    await page.getByRole('tab', { name: '时间分段', exact: true }).click()
    for (const segment of shortResult.output.segments) await expect(detail).toContainText(segment.text.trim())
    await page.getByRole('tab', { name: '转写全文', exact: true }).click()
    const txtPath = await downloaded(page, '下载 TXT', 'delivery-short-transcript.txt')
    const jsonPath = await downloaded(page, '下载 JSON', 'delivery-short-transcript.json')
    expect(await readFile(txtPath, 'utf8')).toBe(shortResult.output.text)
    expect(JSON.parse(await readFile(jsonPath, 'utf8'))).toEqual(shortResult)
    await panel.getByRole('button', { name: '清空令牌', exact: true }).click()
    const layouts: Record<string, unknown>[] = []
    for (const viewport of [{ width: 1920, height: 1080 }, { width: 1366, height: 768 }, { width: 390, height: 844 }]) {
      await page.setViewportSize(viewport)
      await panel.scrollIntoViewIfNeeded()
      await expect(panel.getByLabel('本机管理令牌', { exact: true })).toHaveValue('')
      const documentWidth = await page.evaluate(() => document.documentElement.scrollWidth)
      expect(documentWidth).toBeLessThanOrEqual(viewport.width)
      const screenshot = path.join(out, `delivery-asr-${viewport.width}.png`)
      await page.screenshot({ path: screenshot, fullPage: true })
      layouts.push({ viewport, documentWidth, screenshot, tokenCleared: true })
    }
    await page.setViewportSize({ width: 1366, height: 768 })
    await record('页面全文与分段、TXT/JSON逐值核对、三种屏幕', { txtMatchesStoredText: true, jsonMatchesStoredResult: true, layouts })

    await clickCommand('stop')
    await expect(badge).toHaveText('未启动', { timeout: 60_000 })
    const firstStopped = await status()
    expect(firstStopped.exitConfirmedAt).toBeTruthy()
    expect(firstStopped.acceptingTasks).toBe(false)
    expect((await command('stop')).status()).toBe(202)
    expect((await status()).commandRevision).toBe(firstStopped.commandRevision)
    expect((await sessions()).total).toBe(initialSessions.total + 1)
    const stoppedTxt = await downloaded(page, '下载 TXT', 'delivery-stopped-transcript.txt')
    expect(await readFile(stoppedTxt, 'utf8')).toBe(shortResult.output.text)
    await page.reload()
    await expect(badge).toHaveText('未启动')
    await expect(panel.getByLabel('本机管理令牌', { exact: true })).toHaveValue('')
    await expect(detail).toContainText(shortResult.output.text)
    expect((await request.get('/api/health')).status()).toBe(200)
    expect((await request.get('/')).status()).toBe(200)
    await record('独立停止确认、重复停止安全、历史下载与刷新及 API/页面可用', firstStopped)

    const nav = page.getByRole('navigation', { name: '主导航' })
    await nav.getByRole('button', { name: '图片检测', exact: true }).click()
    const vision = page.getByRole('region', { name: '新建图片检测任务', exact: true })
    await vision.getByLabel('选择图片', { exact: true }).setInputFiles(path.join(base, 'runtime/samples/yolo-bus.jpg'))
    const imageAccepted = page.waitForResponse(response => response.request().method() === 'POST'
      && new URL(response.url()).pathname === '/api/tasks/vision')
    await vision.getByRole('button', { name: '提交检测', exact: true }).click()
    const imageResponse = await imageAccepted
    expect(imageResponse.status()).toBe(201)
    const imageReceipt = (await imageResponse.json()).data as TaskReceipt<VisionTaskView>
    const imageId = imageReceipt.task.taskId
    tasks.Vision = imageReceipt.task
    await record('ASR 停止期间页面提交真实 YOLO 图片', imageReceipt)
    await expect.poll(async () => (await task(imageId)).status, { timeout: 120_000, intervals: [500, 1000] }).toBe('succeeded')
    const imageResult = (await (await request.get(`/api/tasks/${imageId}/result`)).json()).data as TaskResultView<DetectionResult>
    expect(imageResult.output.device).toBe('cpu')
    expect(imageResult.output.detections.length).toBeGreaterThan(0)
    await expect(page.getByRole('button', { name: '下载框图 PNG', exact: true })).toBeEnabled()
    const imageJson = await downloaded(page, '下载 JSON', 'delivery-vision-result.json')
    expect(JSON.parse(await readFile(imageJson, 'utf8'))).toEqual(imageResult)
    const imagePng = await downloaded(page, '下载框图 PNG', 'delivery-vision-annotated.png')
    const authoritativePng = await request.get(`/api/tasks/${imageId}/image?variant=annotated`)
    expect(authoritativePng.status()).toBe(200)
    expect(await readFile(imagePng)).toEqual(await authoritativePng.body())
    const runtime = (await (await request.get('/api/runtime')).json()).data
    expect(runtime.visionWorker.modelReady).toBe(true)
    expect((await status()).state).toBe('stopped')
    tasks.Vision = (await (await request.get(`/api/tasks/${imageId}`)).json()).data
    await record('ASR 停止期间真实 Vision 成功，JSON/PNG与后台结果一致', { result: imageResult, runtime })
    await nav.getByRole('button', { name: '文本生成', exact: true }).click()
    await expect(page.getByRole('button', { name: '开始生成', exact: true })).toBeDisabled()
    const llm = (await (await request.get('/api/llm/status')).json()).data
    expect(llm.configured).toBe(false)
    expect(llm.availability).toBe('unconfigured')
    expect(llm.serviceVerified).toBe(false)
    await record('LLM 保持真实未配置状态，未发起生成或使用模拟回答', llm)
    await nav.getByRole('button', { name: '音频转写', exact: true }).click()
    await expect(badge).toHaveText('未启动')
    await expect(panel.getByLabel('本机管理令牌', { exact: true })).toHaveValue('')

    await clickCommand('start')
    await expect(badge).toHaveText('可以转写', { timeout: 120_000 })
    const secondReady = await status()
    expect(secondReady.workerSessionId).not.toBe(firstReady.workerSessionId)
    expect(secondReady.agentSessionId).toBe(firstReady.agentSessionId)
    expect((await sessions()).total).toBe(initialSessions.total + 2)
    await record('第二次页面启动取得新模型会话，Agent 身份保持', secondReady)
    const longId = await submitAudio(longAudio, '长音频排空')
    await expect.poll(async () => (await status()).activeTaskId, { timeout: 30_000, intervals: [200, 300] }).toBe(longId)
    await expect(badge).toHaveText('正在执行')
    await expect(panel.getByRole('button', { name: `查看当前任务 ${longId}`, exact: true })).toBeVisible()
    const queuedId = await submitAudio(audio, '停止前排队')
    const queuedBefore = await task(queuedId)
    expect(queuedBefore.attempt).toBe(0)
    expect(['created', 'queued']).toContain(queuedBefore.status)
    expect(queuedBefore.queueDeadlineAt).toBeTruthy()
    expect(queuedBefore.totalDeadlineAt).toBeTruthy()
    expect((await status()).activeTaskId).toBe(longId)
    await clickCommand('stop')
    await expect(badge).toHaveText('停止中')
    const draining = await status()
    expect(draining.desiredState).toBe('stopped')
    expect(draining.acceptingTasks).toBe(false)
    expect(draining.activeTaskId).toBe(longId)
    await expect(panel).toContainText('等待当前音频结束并确认进程退出')
    // Reset the upload receipt, then confirm a new UI submission is blocked.
    await page.getByLabel('选择音频', { exact: true }).setInputFiles(longAudio)
    await expect(page.getByRole('button', { name: '提交转写', exact: true })).toBeDisabled()
    await record('页面在途停止：当前音频继续、停止接单与后续领取', { draining, queuedBefore })
    await completed(longId, '长音频排空')
    await expect(badge).toHaveText('未启动', { timeout: 60_000 })
    const secondStopped = await status()
    expect(secondStopped.exitConfirmedAt).toBeTruthy()
    const queuedAfter = await task(queuedId)
    expect(queuedAfter.status).toBe('queued')
    expect(queuedAfter.attempt).toBe(0)
    expect(queuedAfter.startedAt).toBeNull()
    expect(queuedAfter.queueDeadlineAt).toBe(queuedBefore.queueDeadlineAt)
    expect(queuedAfter.totalDeadlineAt).toBe(queuedBefore.totalDeadlineAt)
    await record('当前长音频成功且模型退出，后续任务未领取、原期限保持', { secondStopped, queuedBefore, queuedAfter })

    await clickCommand('start')
    // Existing queued work may move ready -> busy before a two-second UI poll.
    await expect.poll(async () => (await status()).modelReady, { timeout: 120_000, intervals: [500, 1000] }).toBe(true)
    const thirdReady = await status()
    expect(thirdReady.workerSessionId).not.toBe(secondReady.workerSessionId)
    expect((await sessions()).total).toBe(initialSessions.total + 3)
    const queuedResult = await completed(queuedId, '停止前排队')
    const resumed = await task(queuedId)
    expect(resumed.totalDeadlineAt).toBe(queuedBefore.totalDeadlineAt)
    // queueDeadlineAt is retained after claim as the original queue boundary.
    expect(resumed.queueDeadlineAt).toBe(queuedBefore.queueDeadlineAt)
    await expect(detail).toContainText(queuedResult.output.text)
    await record('第三次页面启动恢复原排队任务，成功结果与总期限保持', { thirdReady, resumed })
    await clickCommand('stop')
    await expect(badge).toHaveText('未启动', { timeout: 60_000 })
    const finalStatus = await status()
    const finalSessions = await sessions()
    expect(finalStatus.exitConfirmedAt).toBeTruthy()
    expect(finalSessions.total).toBe(initialSessions.total + 3)
    expect(finalSessions.items.every(item => item.exitConfirmedAt !== null)).toBe(true)
    const stores = await page.evaluate(() => [JSON.stringify(localStorage), JSON.stringify(sessionStorage), window.location.href])
    if (stores.some(value => value.includes(token))) throw new Error('管理令牌出现在浏览器持久化存储或地址；内容已省略。')
    await panel.getByRole('button', { name: '清空令牌', exact: true }).click()
    expect(pageErrors).toEqual([])
    await record('最终 ASR 独立停止、所有受管会话退出确认、无页面异常', { finalStatus, finalSessions, tokenMemoryOnly: true, pageErrors })
    report.passed = true
    report.completedAt = new Date().toISOString()
  } catch (error) {
    report.failedAt = new Date().toISOString()
    report.failure = error instanceof Error ? error.message.split(token).join('[REDACTED]') : '真实浏览器验收未完成'
    if (instanceId) {
      try { report.stateAtFailure = await status() } catch { report.stateAtFailure = '状态查询失败，不能据此判断服务已经停止' }
    }
    // The parent acceptance runner owns whole-platform shutdown and will drain
    // any live audio. Do not delete tasks or force-kill processes to hide failure.
    throw new Error(String(report.failure))
  } finally {
    report.pageErrors = pageErrors
    await save()
  }
})

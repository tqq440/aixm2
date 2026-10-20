/** Browser interaction integration with API substitutes; no model or database is accessed. */
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import App from './App'
import { api, ApiError } from './api/client'
import type { ModelSpec, ResultByType, RuntimeView, TaskReceipt, TaskView, VisionModelSpec, VisionTaskView } from './types/api'
import { readWorkspaceUrl, writeWorkspaceUrl } from './utils/workspace'
import { useAsrManagement } from './hooks/useAsrManagement'
import { useExternalAsr } from './hooks/useExternalAsr'
import { useAsrScheduling } from '@/hooks/useAsrScheduling'
import { catalogFixture, schedulingFixture } from '@/test/asrSchedulingFixtures'

vi.mock('./hooks/useAsrManagement', () => ({ useAsrManagement: vi.fn() }))
vi.mock('./hooks/useExternalAsr', () => ({ useExternalAsr: vi.fn() }))
vi.mock('@/hooks/useAsrScheduling', () => ({ useAsrScheduling: vi.fn() }))

const ASR_ID = 'a'.repeat(32)
const VISION_ID = 'b'.repeat(32)
const asrModel: ModelSpec = {
  modelId: 'whisper-small', modelVersion: 'asr-fixed', modelRepo: 'test/asr', taskType: 'asr', executionMode: 'offline',
  adapterKey: 'faster-whisper', parameterSchema: { defaultLanguage: 'zh', autoDetectionValue: 'auto', languages: ['zh'] },
  inputConstraints: { maxFileBytes: 20 * 1024 * 1024, maxAudioSeconds: 300 }, outputCapabilities: ['text', 'segments'],
}
const visionModel: VisionModelSpec = {
  modelId: 'yolo-general', modelVersion: 'vision-fixed', modelRepo: 'test/vision', taskType: 'vision', executionMode: 'offline',
  adapterKey: 'yolov5-onnx', parameterSchema: { defaultConfidenceThreshold: .25, defaultIouThreshold: .45, defaultMaxDetections: 100, maxDetections: 300 },
  inputConstraints: { maxFileBytes: 10 * 1024 * 1024, maxPixels: 20_000_000, formats: ['JPEG', 'PNG'] }, outputCapabilities: ['detections'],
}
const asrTask: TaskView = {
  taskId: ASR_ID, callerId: 'local-user', taskType: 'asr', modelId: asrModel.modelId, modelVersion: asrModel.modelVersion,
  parameters: { language: 'zh' }, status: 'succeeded', attempt: 1, maxAttempts: 2, executionPlanVersion: 1,
  input: { fileName: 'saved-speech.wav', sizeBytes: 100, sha256: '0'.repeat(64), durationSeconds: 5 },
  createdAt: '2026-10-13T10:00:00Z', updatedAt: '2026-10-13T10:00:01Z', startedAt: null, finishedAt: null,
  queueDeadlineAt: null, totalDeadlineAt: null, attemptDeadlineAt: null, nextRetryAt: null,
  error: null, resultAvailable: true, message: 'completed fixture',
}
const visionTask: VisionTaskView = {
  ...asrTask, taskId: VISION_ID, taskType: 'vision', modelId: visionModel.modelId, modelVersion: visionModel.modelVersion,
  parameters: { confidenceThreshold: .25, iouThreshold: .45, maxDetections: 100 },
  input: { fileName: 'saved-picture.png', sizeBytes: 100, sha256: '1'.repeat(64), mediaType: 'image', width: 200, height: 100 },
}
const asrResult: ResultByType<'asr'> = {
  taskId: ASR_ID, modelId: asrModel.modelId, modelVersion: asrModel.modelVersion, schemaVersion: '1',
  output: { text: '这是音频接口测试文字', segments: [], language: 'zh', languageRequested: 'zh', languageProbability: 1,
    audioDurationSeconds: 5, modelRepo: asrModel.modelRepo, modelRevision: asrModel.modelVersion, device: 'cpu',
    computeType: 'int8', cpuThreads: 4, modelLoadSeconds: 0, transcriptionSeconds: 1, totalSeconds: 1,
    fasterWhisperVersion: 'fixture', ctranslate2Version: 'fixture' },
}
const visionResult: ResultByType<'vision'> = {
  taskId: VISION_ID, modelId: visionModel.modelId, modelVersion: visionModel.modelVersion, schemaVersion: '1',
  output: { modelId: visionModel.modelId, modelVersion: visionModel.modelVersion, modelSha256: '1'.repeat(64),
    imageWidth: 200, imageHeight: 100, coordinateSystem: 'original-pixels-exif-normalized', detections: [],
    device: 'cpu', runtimeVersion: 'fixture', inputType: 'float16', preprocessingVersion: 'fixture',
    loadSeconds: 0, inferenceSeconds: .1, totalSeconds: .2 },
}
const component = { reportedState: 'idle', availability: 'online' as const, ready: true, modelReady: true,
  pid: 123, childPid: 456, activeTaskId: null, heartbeatAt: '2026-10-13T10:00:00Z', heartbeatAgeSeconds: 0, lastError: null }
const runtime: RuntimeView = { observedAt: '2026-10-13T10:00:00Z', apiStatus: 'running',
  worker: { ...component, componentId: 'worker' }, visionWorker: { ...component, componentId: 'vision_worker' },
  dispatcher: { ...component, componentId: 'dispatcher' }, executionConcurrency: 2, automaticExecutionRetry: true, message: 'fixture' }

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason: unknown) => void
  const promise = new Promise<T>((ok, fail) => { resolve = ok; reject = fail })
  return { promise, resolve, reject }
}

const originalCreate = Object.getOwnPropertyDescriptor(URL, 'createObjectURL')
const originalRevoke = Object.getOwnPropertyDescriptor(URL, 'revokeObjectURL')

beforeEach(() => {
  vi.mocked(useAsrScheduling).mockReturnValue({ status: schedulingFixture(), targets: catalogFixture({ total: 0, items: [] }),
    statusError: null, targetsError: null, loading: false, offset: 0, pageSize: 5, now: Date.now(), refresh: vi.fn(),
    setOffset: vi.fn(), statusTrusted: true, targetsTrusted: true })
  vi.mocked(useExternalAsr).mockReturnValue({ status: { configured: false, available: false, state: 'unconfigured', observedAt: null, message: '尚未配置', currentTaskId: null, uncertainTaskId: null, modelId: null, modelVersion: null }, error: null, loading: false, stale: true, refresh: vi.fn() })
  vi.mocked(useAsrManagement).mockReturnValue({
    nodes: [], instances: [], selectedNodeId: null, selectedInstanceId: null, selectedInstance: null,
    selectNode: vi.fn(), selectInstance: vi.fn(), status: {
      instanceId: 'c'.repeat(32), nodeId: 'd'.repeat(32), managementMode: 'node_agent', state: 'ready',
      reportedState: 'ready', desiredState: 'running', agentAvailable: true, agentSessionId: 'e'.repeat(32),
      agentPid: 123, workerSessionId: null, childPid: 456, commandRevision: 0, modelReady: true,
      acceptingTasks: true, activeTaskId: null, lastError: null, heartbeatAt: null, exitConfirmedAt: null,
      shutdownConfirmedAt: null, evaluatedAt: '2026-10-14T02:00:00Z',
    }, loading: false, error: null, commandError: null, commandPending: null, commandNotice: null,
    refresh: vi.fn(), start: vi.fn(), stop: vi.fn(),
  })
  window.history.replaceState(null, '', '/')
  Object.defineProperty(document, 'visibilityState', { configurable: true, value: 'visible' })
  Object.defineProperty(URL, 'createObjectURL', { configurable: true, value: vi.fn(() => 'blob:fixture-image') })
  Object.defineProperty(URL, 'revokeObjectURL', { configurable: true, value: vi.fn() })
  vi.stubGlobal('matchMedia', vi.fn(() => ({ matches: false })))
  vi.spyOn(api, 'runtime').mockResolvedValue(runtime)
  vi.spyOn(api, 'models').mockResolvedValue([asrModel])
  vi.spyOn(api, 'tasks').mockResolvedValue({ items: [asrTask], total: 1, page: 1, pageSize: 20 })
  vi.spyOn(api, 'task').mockResolvedValue(asrTask)
  vi.spyOn(api, 'result').mockResolvedValue(asrResult)
  vi.spyOn(api, 'submit').mockResolvedValue({ task: asrTask, replayed: false })
  vi.spyOn(api, 'cancel').mockResolvedValue({ task: asrTask, decision: 'already_finished' })
  vi.spyOn(api, 'visionModels').mockResolvedValue([visionModel])
  vi.spyOn(api, 'visionTasks').mockResolvedValue({ items: [visionTask], total: 1, page: 1, pageSize: 20 })
  vi.spyOn(api, 'visionTask').mockResolvedValue(visionTask)
  vi.spyOn(api, 'visionResult').mockResolvedValue(visionResult)
  vi.spyOn(api, 'submitVision').mockResolvedValue({ task: visionTask, replayed: false })
  vi.spyOn(api, 'visionCancel').mockResolvedValue({ task: visionTask, decision: 'already_finished' })
  vi.spyOn(api, 'image').mockResolvedValue(new Blob(['fixture'], { type: 'image/png' }))
})

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
  if (originalCreate) Object.defineProperty(URL, 'createObjectURL', originalCreate)
  else Reflect.deleteProperty(URL, 'createObjectURL')
  if (originalRevoke) Object.defineProperty(URL, 'revokeObjectURL', originalRevoke)
  else Reflect.deleteProperty(URL, 'revokeObjectURL')
})

function nav(label: '音频转写' | '图片检测') { return within(screen.getByRole('navigation', { name: '主导航' })).getByRole('button', { name: label }) }
function workspace(kind: 'asr' | 'vision') { return document.querySelector<HTMLDivElement>(`[data-workspace="${kind}"]`)! }

describe('App workspaces', () => {
  it('offers external calls while the local instance is stopped, without controlling the remote engine', async () => {
    const local = vi.mocked(useAsrManagement).getMockImplementation()!(true)
    vi.mocked(useAsrManagement).mockReturnValue({ ...local, status: { ...local.status!, state: 'stopped', acceptingTasks: false } })
    vi.mocked(useExternalAsr).mockReturnValue({ status: { configured: true, available: true, state: 'ready', observedAt: new Date().toISOString(), message: '外部服务已就绪', currentTaskId: null, uncertainTaskId: null, modelId: 'external-fixture', modelVersion: 'fixture-v1' }, error: null, loading: false, stale: false, refresh: vi.fn() })
    const remote = { ...asrModel, modelId: 'external-fixture', modelVersion: 'fixture-v1', adapterKey: 'remote-v2-sensevoice', modelRepo: null }
    vi.mocked(api.models).mockResolvedValue([asrModel, remote])
    render(<App />)
    await screen.findByRole('option', { name: 'external-fixture · 外部服务' })
    const panel = screen.getByRole('region', { name: '外部 ASR 服务' })
    expect(within(panel).queryByRole('button', { name: /启动|停止/ })).toBeNull()
    fireEvent.change(screen.getByLabelText('模型'), { target: { value: JSON.stringify([remote.modelId, remote.modelVersion]) } })
    fireEvent.change(screen.getByLabelText('选择音频'), { target: { files: [new File(['audio'], 'external.wav')] } })
    expect((screen.getByRole('button', { name: '提交转写' }) as HTMLButtonElement).disabled).toBe(false)
    fireEvent.click(screen.getByRole('button', { name: '提交转写' }))
    await waitFor(() => expect(api.submit).toHaveBeenCalledWith(expect.any(File), remote.modelId, remote.modelVersion, null, expect.any(String), expect.any(AbortSignal)))
  })

  it('keeps the local upload available when the external channel has an unknown outcome', async () => {
    vi.mocked(useExternalAsr).mockReturnValue({ status: { configured: true, available: false, state: 'uncertain', observedAt: new Date().toISOString(), message: '等待核对远端请求', currentTaskId: null, uncertainTaskId: ASR_ID, modelId: 'external-fixture', modelVersion: 'fixture-v1' }, error: null, loading: false, stale: false, refresh: vi.fn() })
    render(<App />)
    await screen.findByRole('option', { name: 'Whisper small · 多语言' })
    fireEvent.change(screen.getByLabelText('选择音频'), { target: { files: [new File(['audio'], 'local.wav')] } })
    expect((screen.getByRole('button', { name: '提交转写' }) as HTMLButtonElement).disabled).toBe(false)
    expect(screen.getByText('远端结果未知', { exact: true })).toBeTruthy()
  })
  it('blocks new ASR uploads when stopped while preserving history and Vision use', async () => {
    const management = vi.mocked(useAsrManagement).getMockImplementation()!(true)
    vi.mocked(useAsrManagement).mockReturnValue({ ...management, status: { ...management.status!, state: 'stopped', desiredState: 'stopped', acceptingTasks: false, modelReady: false } })
    render(<App />)
    await screen.findByRole('option', { name: 'Whisper small · 多语言' })
    fireEvent.change(screen.getByLabelText('选择音频'), { target: { files: [new File(['audio'], 'hold.wav')] } })
    expect((screen.getByRole('button', { name: '提交转写' }) as HTMLButtonElement).disabled).toBe(true)
    fireEvent.click(screen.getByRole('button', { name: /查看任务 saved-speech/ }))
    await screen.findByRole('tab', { name: '转写全文' })
    fireEvent.click(nav('图片检测'))
    await screen.findByRole('option', { name: 'YOLO · 通用目标检测' })
    expect(screen.getByRole('heading', { name: '新建图片检测任务' })).toBeTruthy()
  })
  it('switches list, details and runtime context while retaining the other workspace selection', async () => {
    render(<App />)
    fireEvent.click(await screen.findByRole('button', { name: /查看任务 saved-speech/ }))
    await screen.findByRole('tab', { name: '转写全文' })
    expect(readWorkspaceUrl()).toEqual({ taskType: 'asr', taskId: ASR_ID })
    expect(api.visionTasks).not.toHaveBeenCalled()
    fireEvent.click(nav('图片检测'))
    expect(workspace('asr').hidden).toBe(true)
    expect(screen.queryByRole('tab', { name: '转写全文' })).toBeNull()
    expect(screen.queryByRole('button', { name: /查看任务 saved-speech/ })).toBeNull()
    fireEvent.click(await screen.findByRole('button', { name: /查看任务 saved-picture/ }))
    await screen.findByText('检测成功，本次没有发现目标')
    expect(screen.getByText('图片 Worker')).toBeTruthy()
    expect(readWorkspaceUrl()).toEqual({ taskType: 'vision', taskId: VISION_ID })
    await waitFor(() => expect(api.image).toHaveBeenCalledWith(VISION_ID, 'original', expect.any(AbortSignal)))
    const imageSignal = vi.mocked(api.image).mock.calls[0][2]!
    fireEvent.click(nav('音频转写'))
    expect(workspace('vision').hidden).toBe(true)
    expect(workspace('asr').hidden).toBe(false)
    expect(imageSignal.aborted).toBe(true)
    expect(screen.queryByRole('button', { name: /查看任务 saved-picture/ })).toBeNull()
    await screen.findByRole('tab', { name: '转写全文' })
    expect(readWorkspaceUrl()).toEqual({ taskType: 'asr', taskId: ASR_ID })
    expect(within(screen.getByRole('tabpanel')).getByText(asrResult.output.text)).toBeTruthy()
    expect(screen.getByText('转写 Worker')).toBeTruthy()
  })

  it('restores a Vision result after remount and follows popstate back to an ASR result', async () => {
    writeWorkspaceUrl('vision', VISION_ID)
    const first = render(<App />)
    await screen.findByText('检测成功，本次没有发现目标')
    first.unmount()
    render(<App />)
    await screen.findByText('检测成功，本次没有发现目标')
    expect(screen.getByRole('heading', { level: 1 }).textContent).toContain('看见图片中的目标')
    expect(api.task).not.toHaveBeenCalled()
    act(() => {
      writeWorkspaceUrl('asr', ASR_ID)
      window.dispatchEvent(new PopStateEvent('popstate'))
    })
    await screen.findByRole('tab', { name: '转写全文' })
    expect(readWorkspaceUrl()).toEqual({ taskType: 'asr', taskId: ASR_ID })
    expect(workspace('vision').hidden).toBe(true)
    act(() => {
      writeWorkspaceUrl('vision', VISION_ID)
      window.dispatchEvent(new PopStateEvent('popstate'))
    })
    await waitFor(() => expect(workspace('vision').hidden).toBe(false))
    expect(screen.queryByRole('tab', { name: '转写全文' })).toBeNull()
    expect(readWorkspaceUrl()).toEqual({ taskType: 'vision', taskId: VISION_ID })
  })

  it('keeps both chosen files and the original Vision submission key, version and parameters after a lost response', async () => {
    writeWorkspaceUrl('vision', null)
    vi.mocked(api.submitVision).mockRejectedValueOnce(new ApiError('响应丢失', 'NETWORK_ERROR'))
    render(<App />)
    await screen.findByRole('option', { name: 'YOLO · 通用目标检测' })
    const picture = new File([new Uint8Array([137, 80, 78, 71, 13, 10, 26, 10])], 'chosen-picture.png', { type: 'image/png' })
    fireEvent.change(screen.getByLabelText('选择图片'), { target: { files: [picture] } })
    fireEvent.change(screen.getByLabelText('置信度阈值'), { target: { value: '0.6' } })
    await waitFor(() => expect((screen.getByRole('button', { name: '提交检测' }) as HTMLButtonElement).disabled).toBe(false))
    fireEvent.click(screen.getByRole('button', { name: '提交检测' }))
    await screen.findByRole('button', { name: '重试提交' })
    const original = vi.mocked(api.submitVision).mock.calls[0]
    expect(original[3].confidenceThreshold).toBe(.6)
    fireEvent.click(nav('音频转写'))
    await screen.findByRole('option', { name: 'Whisper small · 多语言' })
    const audio = new File(['audio'], 'chosen-audio.wav')
    fireEvent.change(screen.getByLabelText('选择音频'), { target: { files: [audio] } })
    vi.mocked(api.visionModels).mockResolvedValue([{ ...visionModel, modelVersion: 'new-default' }])
    fireEvent.click(nav('图片检测'))
    await waitFor(() => expect(api.visionModels).toHaveBeenCalledTimes(2))
    expect(within(workspace('vision')).getByText('chosen-picture.png')).toBeTruthy()
    expect((screen.getByLabelText('置信度阈值') as HTMLInputElement).value).toBe('0.6')
    fireEvent.click(screen.getByRole('button', { name: '重试提交' }))
    await waitFor(() => expect(api.submitVision).toHaveBeenCalledTimes(2))
    const repeated = vi.mocked(api.submitVision).mock.calls[1]
    expect(repeated[0]).toBe(picture)
    expect(repeated.slice(1, 5)).toEqual(original.slice(1, 5))
    await screen.findByText('检测成功，本次没有发现目标')
    await act(async () => { fireEvent.click(nav('音频转写')) })
    expect(within(workspace('asr')).getByText('chosen-audio.wav')).toBeTruthy()
    expect(api.submit).not.toHaveBeenCalled()
  })

  it('disables navigation while a submit is pending and re-enables it after an uncertain response', async () => {
    const pending = deferred<TaskReceipt>()
    vi.mocked(api.submit).mockReturnValueOnce(pending.promise)
    render(<App />)
    await screen.findByRole('option', { name: 'Whisper small · 多语言' })
    fireEvent.change(screen.getByLabelText('选择音频'), { target: { files: [new File(['audio'], 'pending.wav')] } })
    fireEvent.click(screen.getByRole('button', { name: '提交转写' }))
    expect((nav('图片检测') as HTMLButtonElement).disabled).toBe(true)
    expect((nav('音频转写') as HTMLButtonElement).disabled).toBe(true)
    fireEvent.click(nav('图片检测'))
    expect(workspace('vision').hidden).toBe(true)
    expect(readWorkspaceUrl().taskType).toBe('asr')
    await act(async () => pending.reject(new ApiError('响应丢失', 'NETWORK_ERROR')))
    expect((nav('图片检测') as HTMLButtonElement).disabled).toBe(false)
    fireEvent.click(nav('图片检测'))
    await screen.findByRole('option', { name: 'YOLO · 通用目标检测' })
    expect(readWorkspaceUrl().taskType).toBe('vision')
  })
})

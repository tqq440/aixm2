import { act, cleanup, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { api, ApiError } from '../api/client'
import type { CancellationReceipt, ModelSpec, ResultByType, RuntimeView, TaskPage, TaskResultView, TaskView, VisionModelSpec, VisionParameters, VisionTaskView } from '../types/api'
import { readWorkspaceUrl, writeWorkspaceUrl } from '../utils/workspace'
import { usePlatform } from './usePlatform'

const A = 'a'.repeat(32)
const B = 'b'.repeat(32)
const model: ModelSpec = {
  modelId: 'whisper-small', modelVersion: 'rev', modelRepo: 'Systran/faster-whisper-small',
  taskType: 'asr', executionMode: 'offline', adapterKey: 'faster-whisper',
  parameterSchema: { defaultLanguage: 'zh', autoDetectionValue: 'auto', languages: ['zh', 'en'] },
  inputConstraints: { maxFileBytes: 20 * 1024 * 1024, maxAudioSeconds: 300 },
  outputCapabilities: ['text', 'segments', 'language'],
}
const visionParameters: VisionParameters = { confidenceThreshold: .25, iouThreshold: .45, maxDetections: 100 }
const visionModel: VisionModelSpec = {
  ...model, modelId: 'yolo-general', modelVersion: 'yolov5n-v7.0-onnx', modelRepo: 'ultralytics/yolov5',
  taskType: 'vision', adapterKey: 'yolov5-onnx',
  parameterSchema: { defaultConfidenceThreshold: .25, defaultIouThreshold: .45, defaultMaxDetections: 100, maxDetections: 300 },
  inputConstraints: { maxFileBytes: 10 * 1024 * 1024, maxPixels: 20_000_000, formats: ['JPEG', 'PNG'] },
  outputCapabilities: ['detections', 'imageDimensions'],
}
const component = {
  reportedState: 'idle', availability: 'online' as const, ready: true, modelReady: true,
  pid: 123, childPid: null, activeTaskId: null, heartbeatAt: '2026-10-12T10:00:00Z',
  heartbeatAgeSeconds: 0, lastError: null,
}
const runtime: RuntimeView = {
  observedAt: '2026-10-12T10:00:00Z', apiStatus: 'running',
  worker: { componentId: 'worker', ...component }, dispatcher: { componentId: 'dispatcher', ...component },
  executionConcurrency: 1, automaticExecutionRetry: true, message: 'actual process reports',
}

function task(id = A, status: TaskView['status'] = 'succeeded'): TaskView {
  return {
    taskId: id, callerId: 'local-user', taskType: 'asr', modelId: 'whisper-small', modelVersion: 'rev',
    parameters: { language: 'zh' }, status, attempt: status === 'created' ? 0 : 1,
    executionPlanVersion: 1, input: { fileName: 'speech.wav', sizeBytes: 100, sha256: '0'.repeat(64), durationSeconds: 5 },
    createdAt: '2026-10-12T10:00:00Z', updatedAt: '2026-10-12T10:00:01Z',
    startedAt: null, finishedAt: null, error: null, resultAvailable: status === 'succeeded', message: status,
    maxAttempts: 2, queueDeadlineAt: '2026-10-12T10:10:00Z', totalDeadlineAt: '2026-10-12T10:30:00Z',
    attemptDeadlineAt: null, nextRetryAt: null,
  }
}

function page(items = [task()], value = 1): TaskPage {
  return { items, total: 30, page: value, pageSize: 20 }
}

function result(id = A): TaskResultView {
  return {
    taskId: id, modelId: 'whisper-small', modelVersion: 'rev', schemaVersion: '1',
    output: {
      text: `真实结果 ${id}`, segments: [{ start: 0, end: 5, text: '真实结果' }], language: 'zh',
      languageRequested: 'zh', languageProbability: 1, audioDurationSeconds: 5,
      modelRepo: 'Systran/faster-whisper-small', modelRevision: 'rev', device: 'cpu',
      computeType: 'int8_float32', cpuThreads: 4, modelLoadSeconds: 0,
      transcriptionSeconds: 1.5, totalSeconds: 1.6, fasterWhisperVersion: 'test', ctranslate2Version: 'test',
    },
  }
}

function visionTask(id = A, status: TaskView['status'] = 'succeeded'): VisionTaskView {
  return { ...task(id, status), taskType: 'vision', modelId: 'yolo-general', modelVersion: visionModel.modelVersion,
    parameters: visionParameters, input: { fileName: 'example.png', sizeBytes: 100, sha256: '0'.repeat(64), mediaType: 'image', width: 200, height: 100 } }
}

function visionResult(id = A): ResultByType<'vision'> {
  return { taskId: id, modelId: 'yolo-general', modelVersion: visionModel.modelVersion, schemaVersion: '1', output: {
    modelId: 'yolo-general', modelVersion: visionModel.modelVersion, modelSha256: '0'.repeat(64),
    imageWidth: 200, imageHeight: 100, coordinateSystem: 'original-pixels-exif-normalized', detections: [],
    device: 'cpu', runtimeVersion: 'test', inputType: 'float16', preprocessingVersion: 'test',
    loadSeconds: 0, inferenceSeconds: .1, totalSeconds: .2,
  } }
}

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason: unknown) => void
  const promise = new Promise<T>((ok, fail) => { resolve = ok; reject = fail })
  return { promise, resolve, reject }
}

async function settle() { await act(async () => { await Promise.resolve() }) }
async function advance(ms = 2_000) { await act(async () => { await vi.advanceTimersByTimeAsync(ms) }) }
function visibility(value: 'visible' | 'hidden') {
  Object.defineProperty(document, 'visibilityState', { configurable: true, value })
  document.dispatchEvent(new Event('visibilitychange'))
}

beforeEach(() => {
  vi.useFakeTimers()
  window.history.replaceState(null, '', '/')
  Object.defineProperty(document, 'visibilityState', { configurable: true, value: 'visible' })
  vi.spyOn(api, 'models').mockResolvedValue([model])
  vi.spyOn(api, 'runtime').mockResolvedValue(runtime)
  vi.spyOn(api, 'tasks').mockResolvedValue(page())
  vi.spyOn(api, 'task').mockImplementation(async id => task(id))
  vi.spyOn(api, 'result').mockImplementation(async id => result(id))
  vi.spyOn(api, 'submit').mockResolvedValue({ task: task(), replayed: false })
  vi.spyOn(api, 'cancel').mockResolvedValue({ task: task(A, 'cancel_requested'), decision: 'cancel_requested' })
  vi.spyOn(api, 'visionModels').mockResolvedValue([visionModel])
  vi.spyOn(api, 'visionTasks').mockResolvedValue({ items: [visionTask()], total: 1, page: 1, pageSize: 20 })
  vi.spyOn(api, 'visionTask').mockImplementation(async id => visionTask(id))
  vi.spyOn(api, 'visionResult').mockImplementation(async id => visionResult(id))
  vi.spyOn(api, 'submitVision').mockResolvedValue({ task: visionTask(), replayed: false })
  vi.spyOn(api, 'visionCancel').mockResolvedValue({ task: visionTask(A, 'cancel_requested'), decision: 'cancel_requested' })
})

afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.useRealTimers() })

describe('usePlatform polling and selection', () => {
  it('restores a task from the URL and stops polling finished details and results', async () => {
    window.history.replaceState(null, '', `/?task=${A}`)
    const hook = renderHook(() => usePlatform())
    await settle()
    expect(hook.result.current.result?.taskId).toBe(A)
    expect(hook.result.current.loading).toBe(false)
    await advance()
    expect(api.runtime).toHaveBeenCalledTimes(2)
    expect(api.models).toHaveBeenCalledTimes(1)
    expect(api.tasks).toHaveBeenCalledTimes(1)
    expect(api.task).toHaveBeenCalledTimes(1)
    expect(api.result).toHaveBeenCalledTimes(1)
    act(() => hook.result.current.refresh())
    await settle()
    expect(api.models).toHaveBeenCalledTimes(2)
    expect(api.result).toHaveBeenCalledTimes(2)
  })

  it('polls unfinished tasks every two seconds without inventing a final result', async () => {
    vi.mocked(api.tasks).mockResolvedValue(page([task(A, 'running')]))
    vi.mocked(api.task).mockResolvedValue(task(A, 'running'))
    window.history.replaceState(null, '', `/?task=${A}`)
    const hook = renderHook(() => usePlatform())
    await settle()
    await advance(1_999)
    expect(api.task).toHaveBeenCalledTimes(1)
    await advance(1)
    expect(api.task).toHaveBeenCalledTimes(2)
    expect(api.tasks).toHaveBeenCalledTimes(2)
    expect(api.result).not.toHaveBeenCalled()
    expect(hook.result.current.result).toBeNull()
  })

  it('keeps a loaded result visible when the already selected task is clicked again', async () => {
    window.history.replaceState(null, '', `/?task=${A}`)
    const hook = renderHook(() => usePlatform())
    await settle()
    const loadedResult = hook.result.current.result
    act(() => hook.result.current.selectTask(A))
    expect(hook.result.current.task?.taskId).toBe(A)
    expect(hook.result.current.result).toBe(loadedResult)
    expect(hook.result.current.selectedLoading).toBe(false)
    await advance()
    expect(api.task).toHaveBeenCalledTimes(1)
    expect(api.result).toHaveBeenCalledTimes(1)
  })

  it('aborts reads and stops timers while hidden, then refreshes on return', async () => {
    const waiting = deferred<RuntimeView>()
    vi.mocked(api.runtime).mockReturnValueOnce(waiting.promise)
    const hook = renderHook(() => usePlatform())
    await settle()
    const oldSignal = vi.mocked(api.runtime).mock.calls[0][0]!
    act(() => visibility('hidden'))
    expect(oldSignal.aborted).toBe(true)
    await advance(10_000)
    expect(api.runtime).toHaveBeenCalledTimes(1)
    act(() => visibility('visible'))
    await settle()
    expect(api.runtime).toHaveBeenCalledTimes(2)
    await act(async () => waiting.resolve({ ...runtime, observedAt: 'old response' }))
    expect(hook.result.current.runtime?.observedAt).toBe(runtime.observedAt)
  })

  it('never overlaps polling during a slow request, and unmount aborts it', async () => {
    vi.mocked(api.runtime).mockReturnValue(deferred<RuntimeView>().promise)
    const hook = renderHook(() => usePlatform())
    await settle()
    await advance(20_000)
    expect(api.runtime).toHaveBeenCalledTimes(1)
    const signal = vi.mocked(api.runtime).mock.calls[0][0]!
    hook.unmount()
    expect(signal.aborted).toBe(true)
    await advance(20_000)
    expect(api.runtime).toHaveBeenCalledTimes(1)
  })

  it('stops on pagehide and restores on pageshow for browser back/forward cache', async () => {
    renderHook(() => usePlatform())
    await settle()
    act(() => window.dispatchEvent(new Event('pagehide')))
    await advance(10_000)
    expect(api.runtime).toHaveBeenCalledTimes(1)
    act(() => window.dispatchEvent(new Event('pageshow')))
    await settle()
    expect(api.runtime).toHaveBeenCalledTimes(2)
  })

  it('ignores the old result when the user selects another task', async () => {
    const oldResult = deferred<TaskResultView>()
    vi.mocked(api.result).mockImplementation(id => id === A ? oldResult.promise : Promise.resolve(result(id)))
    window.history.replaceState(null, '', `/?task=${A}`)
    const hook = renderHook(() => usePlatform())
    await settle()
    act(() => hook.result.current.selectTask(B))
    await settle()
    expect(hook.result.current.task?.taskId).toBe(B)
    expect(hook.result.current.result?.taskId).toBe(B)
    await act(async () => oldResult.resolve(result(A)))
    expect(hook.result.current.result?.taskId).toBe(B)
    expect(new URL(window.location.href).searchParams.get('task')).toBe(B)
  })

  it('ignores an old page arriving after a newer page', async () => {
    const oldPage = deferred<TaskPage>()
    vi.mocked(api.tasks).mockImplementation(value => value === 1 ? oldPage.promise : Promise.resolve(page([task(B)], 2)))
    const hook = renderHook(() => usePlatform())
    await settle()
    act(() => hook.result.current.setPage(2))
    await settle()
    await act(async () => oldPage.resolve(page()))
    expect(hook.result.current.tasks?.page).toBe(2)
    expect(hook.result.current.tasks?.items[0].taskId).toBe(B)
  })

  it('exposes actual API errors without filling a missing task or result', async () => {
    const error = new ApiError('任务不存在。', 'TASK_NOT_FOUND', 404, 'request-123')
    vi.mocked(api.task).mockRejectedValue(error)
    window.history.replaceState(null, '', `/?task=${A}`)
    const hook = renderHook(() => usePlatform())
    await settle()
    expect(hook.result.current.taskError).toBe(error)
    expect(hook.result.current.task).toBeNull()
    expect(hook.result.current.result).toBeNull()
    expect(api.result).not.toHaveBeenCalled()
  })
})

describe('usePlatform cancellation', () => {
  beforeEach(() => {
    window.history.replaceState(null, '', `/?task=${A}`)
    vi.mocked(api.tasks).mockResolvedValue(page([task(A, 'running')]))
    vi.mocked(api.task).mockResolvedValue(task(A, 'running'))
  })

  it('blocks double clicks, keeps polling cancel_requested, and stops at confirmed cancelled', async () => {
    const pending = deferred<CancellationReceipt>()
    vi.mocked(api.cancel).mockReturnValue(pending.promise)
    const hook = renderHook(() => usePlatform())
    await settle()
    let first!: Promise<unknown>
    act(() => {
      first = hook.result.current.cancelTask()
      void hook.result.current.cancelTask()
    })
    expect(api.cancel).toHaveBeenCalledTimes(1)
    expect(hook.result.current.cancellation?.pending).toBe(true)
    expect(hook.result.current.task?.status).toBe('running')
    vi.mocked(api.task).mockResolvedValue(task(A, 'cancel_requested'))
    vi.mocked(api.tasks).mockResolvedValue(page([task(A, 'cancel_requested')]))
    await act(async () => { pending.resolve({ task: task(A, 'cancel_requested'), decision: 'cancel_requested' }); await first })
    await settle()
    expect(hook.result.current.task?.status).toBe('cancel_requested')
    await act(async () => { await hook.result.current.cancelTask() })
    expect(api.cancel).toHaveBeenCalledTimes(1)
    const reads = vi.mocked(api.task).mock.calls.length
    await advance()
    expect(api.task).toHaveBeenCalledTimes(reads + 1)
    vi.mocked(api.task).mockResolvedValue(task(A, 'cancelled'))
    vi.mocked(api.tasks).mockResolvedValue(page([task(A, 'cancelled')]))
    await advance()
    expect(hook.result.current.task?.status).toBe('cancelled')
    const finalReads = vi.mocked(api.task).mock.calls.length
    const finalListReads = vi.mocked(api.tasks).mock.calls.length
    await advance(6_000)
    expect(api.task).toHaveBeenCalledTimes(finalReads)
    expect(api.tasks).toHaveBeenCalledTimes(finalListReads)
    expect(api.result).not.toHaveBeenCalled()
  })

  it('reads the winning success and result when cancellation races with result persistence', async () => {
    const hook = renderHook(() => usePlatform())
    await settle()
    vi.mocked(api.cancel).mockResolvedValue({ task: task(), decision: 'already_finished' })
    vi.mocked(api.task).mockResolvedValue(task())
    await act(async () => { await hook.result.current.cancelTask() })
    await settle()
    expect(hook.result.current.task?.status).toBe('succeeded')
    expect(hook.result.current.result?.taskId).toBe(A)
    expect(hook.result.current.cancellation?.decision).toBe('already_finished')
    await act(async () => { await hook.result.current.cancelTask() })
    expect(api.cancel).toHaveBeenCalledTimes(1)
  })

  it('does not invent cancellation after a lost response and allows a safe retry', async () => {
    const failure = new ApiError('连接中断', 'NETWORK_ERROR')
    vi.mocked(api.cancel).mockRejectedValueOnce(failure)
    const hook = renderHook(() => usePlatform())
    await settle()
    await act(async () => { await hook.result.current.cancelTask() })
    await settle()
    expect(hook.result.current.task?.status).toBe('running')
    expect(hook.result.current.cancellation).toMatchObject({ error: failure, uncertain: true, pending: false })
    vi.mocked(api.cancel).mockResolvedValue({ task: task(A, 'cancelled'), decision: 'cancelled' })
    vi.mocked(api.task).mockResolvedValue(task(A, 'cancelled'))
    await act(async () => { await hook.result.current.cancelTask() })
    await settle()
    expect(api.cancel).toHaveBeenCalledTimes(2)
    expect(hook.result.current.task?.status).toBe('cancelled')
    expect(hook.result.current.cancellation).toMatchObject({ error: null, uncertain: false })
  })

  it.each(['success', 'failure'] as const)('isolates a late cancellation %s after selecting another task', async outcome => {
    const pending = deferred<CancellationReceipt>()
    vi.mocked(api.cancel).mockReturnValue(pending.promise)
    vi.mocked(api.task).mockImplementation(async id => task(id, id === A ? 'running' : 'succeeded'))
    const hook = renderHook(() => usePlatform())
    await settle()
    act(() => { void hook.result.current.cancelTask() })
    act(() => hook.result.current.selectTask(B))
    await settle()
    await act(async () => {
      if (outcome === 'success') pending.resolve({ task: task(A, 'cancel_requested'), decision: 'cancel_requested' })
      else pending.reject(new ApiError('旧任务请求失败', 'NETWORK_ERROR'))
    })
    await settle()
    expect(hook.result.current.task?.taskId).toBe(B)
    expect(hook.result.current.result?.taskId).toBe(B)
    expect(hook.result.current.cancellation).toBeNull()
    expect(hook.result.current.selectedTaskId).toBe(B)
    expect(new URL(window.location.href).searchParams.get('task')).toBe(B)
  })

  it('does not let a read started before cancellation replace its refreshed final state', async () => {
    const hook = renderHook(() => usePlatform())
    await settle()
    const oldRead = deferred<TaskView>()
    vi.mocked(api.task).mockReturnValueOnce(oldRead.promise)
    await advance()
    vi.mocked(api.task).mockResolvedValue(task(A, 'cancelled'))
    vi.mocked(api.cancel).mockResolvedValue({ task: task(A, 'cancelled'), decision: 'cancelled' })
    await act(async () => { await hook.result.current.cancelTask() })
    await settle()
    expect(hook.result.current.task?.status).toBe('cancelled')
    await act(async () => oldRead.resolve(task(A, 'running')))
    expect(hook.result.current.task?.status).toBe('cancelled')
  })

  it('retains the per-task in-flight guard after switching away and back', async () => {
    const pending = deferred<CancellationReceipt>()
    vi.mocked(api.cancel).mockReturnValue(pending.promise)
    vi.mocked(api.task).mockImplementation(async id => task(id, 'running'))
    const hook = renderHook(() => usePlatform())
    await settle()
    act(() => { void hook.result.current.cancelTask() })
    act(() => hook.result.current.selectTask(B))
    await settle()
    expect(hook.result.current.cancellation).toBeNull()
    act(() => hook.result.current.selectTask(A))
    await settle()
    expect(hook.result.current.cancellation?.pending).toBe(true)
    await act(async () => { await hook.result.current.cancelTask() })
    expect(api.cancel).toHaveBeenCalledTimes(1)
    await act(async () => pending.resolve({ task: task(A, 'cancel_requested'), decision: 'cancel_requested' }))
  })

  it('aborts on pagehide, reports an unknown outcome, and resumes backend reads on return', async () => {
    vi.mocked(api.cancel).mockImplementationOnce((_id, signal) => new Promise((_ok, fail) => {
      signal?.addEventListener('abort', () => fail(new ApiError('请求已中断。', 'REQUEST_ABORTED')))
    }))
    const hook = renderHook(() => usePlatform())
    await settle()
    act(() => { void hook.result.current.cancelTask() })
    act(() => window.dispatchEvent(new Event('pagehide')))
    await settle()
    expect(vi.mocked(api.cancel).mock.calls[0][1]?.aborted).toBe(true)
    expect(hook.result.current.cancellation?.uncertain).toBe(true)
    const runtimeReads = vi.mocked(api.runtime).mock.calls.length
    await advance(10_000)
    expect(api.runtime).toHaveBeenCalledTimes(runtimeReads)
    vi.mocked(api.task).mockResolvedValue(task(A, 'cancelled'))
    act(() => window.dispatchEvent(new Event('pageshow')))
    await settle()
    expect(hook.result.current.task?.status).toBe('cancelled')
  })
})

describe('usePlatform submission identity', () => {
  it('keeps the pinned version and key through catalog refreshes, and assigns a new key to a deliberate version change', async () => {
    vi.mocked(api.submit).mockRejectedValue(new ApiError('响应丢失', 'NETWORK_ERROR'))
    const hook = renderHook(() => usePlatform())
    const file = new File(['audio'], 'speech.wav')
    await settle()
    await act(async () => { await hook.result.current.submit(file, 'whisper-small', 'rev', 'zh') })
    vi.mocked(api.models).mockResolvedValue([{ ...model, modelVersion: 'next-rev' }])
    act(() => hook.result.current.refresh())
    await settle()
    await act(async () => { await hook.result.current.submit(file, 'whisper-small', 'rev', 'zh') })
    await act(async () => { await hook.result.current.submit(file, 'whisper-small', 'next-rev', 'zh') })
    const calls = vi.mocked(api.submit).mock.calls
    expect(calls.map(call => call[2])).toEqual(['rev', 'rev', 'next-rev'])
    expect(calls[0][4]).toBe(calls[1][4])
    expect(calls[2][4]).not.toBe(calls[0][4])
  })

  it('continues reading an existing result when the catalog has no enabled models', async () => {
    vi.mocked(api.models).mockResolvedValue([])
    window.history.replaceState(null, '', `/?task=${A}`)
    const hook = renderHook(() => usePlatform())
    await settle()
    expect(hook.result.current.models).toEqual([])
    expect(hook.result.current.task?.taskId).toBe(A)
    expect(hook.result.current.result?.taskId).toBe(A)
    expect(hook.result.current.resultError).toBeNull()
  })

  it.each([
    new ApiError('连接中断', 'NETWORK_ERROR'),
    new ApiError('数据库忙', 'DATABASE_UNAVAILABLE', 503, 'request-503'),
  ])('retries an uncertain submission with exactly the original idempotency key: %s', async failure => {
    vi.mocked(api.submit).mockRejectedValueOnce(failure)
    const hook = renderHook(() => usePlatform())
    const file = new File(['audio'], 'speech.wav')
    await settle()
    await act(async () => { await hook.result.current.submit(file, 'whisper-small', 'rev', 'zh') })
    expect(hook.result.current.submissionUncertain).toBe(true)
    expect(hook.result.current.submissionError).toBe(failure)
    await act(async () => { await hook.result.current.submit(file, 'whisper-small', 'rev', 'zh') })
    const calls = vi.mocked(api.submit).mock.calls
    expect(calls).toHaveLength(2)
    expect(calls[0][4]).toBe(calls[1][4])
    expect(hook.result.current.submissionUncertain).toBe(false)
    expect(hook.result.current.submissionReceipt?.task.taskId).toBe(A)
    await act(async () => { await hook.result.current.submit(file, 'whisper-small', 'rev', 'zh') })
    expect(api.submit).toHaveBeenCalledTimes(2)
  })

  it('blocks simultaneous double clicks and creates a new key only after a deliberate reset', async () => {
    const pending = deferred<{ task: TaskView; replayed: boolean }>()
    vi.mocked(api.submit).mockReturnValueOnce(pending.promise)
    const hook = renderHook(() => usePlatform())
    const file = new File(['audio'], 'speech.wav')
    await settle()
    let first!: Promise<unknown>
    act(() => {
      first = hook.result.current.submit(file, 'whisper-small', 'rev', 'zh')
      void hook.result.current.submit(file, 'whisper-small', 'rev', 'zh')
      hook.result.current.clearSubmission()
    })
    expect(api.submit).toHaveBeenCalledTimes(1)
    await act(async () => { pending.resolve({ task: task(), replayed: false }); await first })
    const key = vi.mocked(api.submit).mock.calls[0][4]
    act(() => hook.result.current.clearSubmission())
    await act(async () => { await hook.result.current.submit(file, 'whisper-small', 'rev', 'zh') })
    expect(api.submit).toHaveBeenCalledTimes(2)
    expect(vi.mocked(api.submit).mock.calls[1][4]).not.toBe(key)
  })

  it('retains a key when leaving the page interrupts the upload', async () => {
    vi.mocked(api.submit).mockImplementationOnce((_file, _model, _version, _language, _key, signal) => new Promise((_ok, fail) => {
      signal?.addEventListener('abort', () => fail(new ApiError('请求已中断。', 'REQUEST_ABORTED')))
    }))
    const hook = renderHook(() => usePlatform())
    const file = new File(['audio'], 'speech.wav')
    await settle()
    act(() => { void hook.result.current.submit(file, 'whisper-small', 'rev', 'zh') })
    act(() => window.dispatchEvent(new Event('pagehide')))
    await settle()
    expect(hook.result.current.submissionUncertain).toBe(true)
    act(() => window.dispatchEvent(new Event('pageshow')))
    await act(async () => { await hook.result.current.submit(file, 'whisper-small', 'rev', 'zh') })
    const calls = vi.mocked(api.submit).mock.calls
    expect(calls[0][4]).toBe(calls[1][4])
  })
})

describe('usePlatform vision workspace', () => {
  it('restores Vision links and typed empty detection results without querying ASR', async () => {
    writeWorkspaceUrl('vision', A)
    const hook = renderHook(() => usePlatform('vision'))
    await settle()
    expect(hook.result.current.models[0].inputConstraints.maxPixels).toBe(20_000_000)
    expect(hook.result.current.task?.input.width).toBe(200)
    expect(hook.result.current.result?.output.detections).toEqual([])
    expect(api.visionModels).toHaveBeenCalledTimes(1)
    expect(api.models).not.toHaveBeenCalled()
    expect(api.tasks).not.toHaveBeenCalled()
    expect(api.task).not.toHaveBeenCalled()
    expect(api.result).not.toHaveBeenCalled()
    await advance()
    expect(api.visionResult).toHaveBeenCalledTimes(1)
    expect(api.visionTask).toHaveBeenCalledTimes(1)
  })

  it('pins a parameter snapshot, version and key through uncertain Vision submissions', async () => {
    writeWorkspaceUrl('vision', null)
    const pending = deferred<{ task: VisionTaskView; replayed: boolean }>()
    vi.mocked(api.submitVision).mockReturnValueOnce(pending.promise)
    const hook = renderHook(() => usePlatform('vision'))
    const file = new File(['image'], 'example.png', { type: 'image/png' })
    const parameters = { ...visionParameters }
    await settle()
    act(() => { void hook.result.current.submit(file, 'yolo-general', 'fixed-version', parameters) })
    parameters.maxDetections = 20
    expect(vi.mocked(api.submitVision).mock.calls[0][3].maxDetections).toBe(100)
    await act(async () => pending.reject(new ApiError('响应丢失', 'NETWORK_ERROR')))
    expect(hook.result.current.submissionUncertain).toBe(true)
    vi.mocked(api.submitVision).mockRejectedValue(new ApiError('仍未确认', 'NETWORK_ERROR'))
    const reordered = { maxDetections: 100, iouThreshold: .45, confidenceThreshold: .25 }
    await act(async () => { await hook.result.current.submit(file, 'yolo-general', 'fixed-version', reordered) })
    await act(async () => { await hook.result.current.submit(file, 'yolo-general', 'fixed-version', parameters) })
    const calls = vi.mocked(api.submitVision).mock.calls
    expect(calls[1][4]).toBe(calls[0][4])
    expect(calls[2][4]).not.toBe(calls[0][4])
    expect(calls.map(call => call[2])).toEqual(['fixed-version', 'fixed-version', 'fixed-version'])
    expect(api.submit).not.toHaveBeenCalled()
  })

  it('keeps both workspaces mounted but only polls the active type and restores each selection', async () => {
    writeWorkspaceUrl('asr', A)
    const hook = renderHook(({ mode }: { mode: 'asr' | 'vision' }) => ({
      asr: usePlatform('asr', mode === 'asr'), vision: usePlatform('vision', mode === 'vision'),
    }), { initialProps: { mode: 'asr' } })
    await settle()
    expect(hook.result.current.asr.result?.taskId).toBe(A)
    expect(api.visionModels).not.toHaveBeenCalled()
    expect(api.visionTasks).not.toHaveBeenCalled()
    const asrSignal = vi.mocked(api.runtime).mock.calls[0][0]!
    act(() => writeWorkspaceUrl('vision', B, true))
    hook.rerender({ mode: 'vision' })
    await settle()
    expect(asrSignal.aborted).toBe(true)
    expect(hook.result.current.vision.result?.taskId).toBe(B)
    expect(hook.result.current.asr.selectedTaskId).toBe(A)
    const asrReads = vi.mocked(api.task).mock.calls.length
    await advance(6_000)
    expect(api.task).toHaveBeenCalledTimes(asrReads)
    act(() => writeWorkspaceUrl('asr', A, true))
    hook.rerender({ mode: 'asr' })
    await settle()
    expect(hook.result.current.asr.result?.taskId).toBe(A)
    expect(hook.result.current.vision.selectedTaskId).toBe(B)
    expect(api.models).toHaveBeenCalledTimes(2)
    expect(readWorkspaceUrl()).toEqual({ taskType: 'asr', taskId: A })
  })

  it('ignores inactive stale reads even when the transport ignores abort', async () => {
    writeWorkspaceUrl('vision', A)
    const delayed = deferred<ResultByType<'vision'>>()
    vi.mocked(api.visionResult).mockReturnValueOnce(delayed.promise)
    const hook = renderHook(({ active }) => usePlatform('vision', active), { initialProps: { active: true } })
    await settle()
    const signal = vi.mocked(api.visionResult).mock.calls[0][1]!
    act(() => writeWorkspaceUrl('asr', B, true))
    hook.rerender({ active: false })
    await act(async () => delayed.resolve(visionResult(A)))
    expect(signal.aborted).toBe(true)
    expect(hook.result.current.result).toBeNull()
    expect(readWorkspaceUrl()).toEqual({ taskType: 'asr', taskId: B })
    const reads = vi.mocked(api.runtime).mock.calls.length
    act(() => window.dispatchEvent(new Event('pageshow')))
    await advance(10_000)
    expect(api.runtime).toHaveBeenCalledTimes(reads)
    act(() => writeWorkspaceUrl('vision', B, true))
    hook.rerender({ active: true })
    await settle()
    expect(hook.result.current.result?.taskId).toBe(B)
  })

  it('retains an interrupted upload key and never lets its late success change another workspace URL', async () => {
    writeWorkspaceUrl('vision', null)
    const delayed = deferred<{ task: VisionTaskView; replayed: boolean }>()
    vi.mocked(api.submitVision).mockReturnValueOnce(delayed.promise)
    const hook = renderHook(({ active }) => usePlatform('vision', active), { initialProps: { active: true } })
    const file = new File(['image'], 'example.png')
    await settle()
    act(() => { void hook.result.current.submit(file, 'yolo-general', 'fixed', visionParameters) })
    act(() => writeWorkspaceUrl('asr', B, true))
    hook.rerender({ active: false })
    await act(async () => delayed.resolve({ task: visionTask(A), replayed: false }))
    expect(vi.mocked(api.submitVision).mock.calls[0][5]?.aborted).toBe(true)
    expect(hook.result.current.submissionReceipt).toBeNull()
    expect(hook.result.current.submissionUncertain).toBe(true)
    expect(readWorkspaceUrl()).toEqual({ taskType: 'asr', taskId: B })
    act(() => writeWorkspaceUrl('vision', null, true))
    hook.rerender({ active: true })
    await settle()
    await act(async () => { await hook.result.current.submit(file, 'yolo-general', 'fixed', visionParameters) })
    const calls = vi.mocked(api.submitVision).mock.calls
    expect(calls[1][4]).toBe(calls[0][4])
    expect(hook.result.current.submissionReceipt?.task.taskType).toBe('vision')
    expect(readWorkspaceUrl()).toEqual({ taskType: 'vision', taskId: A })
  })

  it('only the matching active workspace applies popstate selection', async () => {
    writeWorkspaceUrl('vision', A)
    const hook = renderHook(({ mode }: { mode: 'asr' | 'vision' }) => ({
      asr: usePlatform('asr', mode === 'asr'), vision: usePlatform('vision', mode === 'vision'),
    }), { initialProps: { mode: 'vision' } })
    await settle()
    act(() => {
      writeWorkspaceUrl('vision', B)
      window.dispatchEvent(new PopStateEvent('popstate'))
    })
    await settle()
    expect(hook.result.current.vision.selectedTaskId).toBe(B)
    expect(hook.result.current.asr.selectedTaskId).toBeNull()
    act(() => {
      writeWorkspaceUrl('asr', A)
      window.dispatchEvent(new PopStateEvent('popstate'))
    })
    hook.rerender({ mode: 'asr' })
    await settle()
    expect(hook.result.current.asr.result?.taskId).toBe(A)
    expect(hook.result.current.vision.selectedTaskId).toBe(B)
    expect(readWorkspaceUrl()).toEqual({ taskType: 'asr', taskId: A })
  })

  it('uses Vision cancellation and keeps reading until backend confirms cancellation', async () => {
    writeWorkspaceUrl('vision', A)
    vi.mocked(api.visionTask).mockResolvedValue(visionTask(A, 'running'))
    vi.mocked(api.visionTasks).mockResolvedValue({ items: [visionTask(A, 'running')], total: 1, page: 1, pageSize: 20 })
    const hook = renderHook(() => usePlatform('vision'))
    await settle()
    vi.mocked(api.visionTask).mockResolvedValue(visionTask(A, 'cancel_requested'))
    await act(async () => { await hook.result.current.cancelTask() })
    await settle()
    expect(api.cancel).not.toHaveBeenCalled()
    expect(api.visionCancel).toHaveBeenCalledTimes(1)
    expect(hook.result.current.task?.status).toBe('cancel_requested')
    vi.mocked(api.visionTask).mockResolvedValue(visionTask(A, 'cancelled'))
    await advance()
    expect(hook.result.current.task?.status).toBe('cancelled')
    expect(api.visionResult).not.toHaveBeenCalled()
  })

  it('preserves an uncertain cancelled request while hidden without altering the other workspace', async () => {
    writeWorkspaceUrl('vision', A)
    vi.mocked(api.visionTask).mockResolvedValue(visionTask(A, 'running'))
    const delayed = deferred<CancellationReceipt<VisionTaskView>>()
    vi.mocked(api.visionCancel).mockReturnValueOnce(delayed.promise)
    const hook = renderHook(({ active }) => usePlatform('vision', active), { initialProps: { active: true } })
    await settle()
    act(() => { void hook.result.current.cancelTask() })
    act(() => writeWorkspaceUrl('asr', B))
    hook.rerender({ active: false })
    await act(async () => delayed.resolve({ task: visionTask(A, 'cancelled'), decision: 'cancelled' }))
    expect(hook.result.current.cancellation).toMatchObject({ pending: false, uncertain: true, decision: null })
    expect(hook.result.current.task?.status).toBe('running')
    expect(readWorkspaceUrl()).toEqual({ taskType: 'asr', taskId: B })
    expect(vi.mocked(api.visionCancel).mock.calls[0][1]?.aborted).toBe(true)
  })
})

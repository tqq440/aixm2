import type { CancellationReceipt, DetectionResult, ModelSpec, RuntimeView, TaskPage, TaskReceipt, TaskResultView, TaskView, ValidationIssue, VisionModelSpec, VisionParameters, VisionTaskView } from '../types/api'
import { validTaskRouting } from '@/utils/asrScheduling'

/** An HTTP error or a client-side transport failure; requestId may be unavailable. */
export class ApiError extends Error {
  readonly code: string
  readonly requestId: string | null
  readonly status: number
  readonly details: ValidationIssue[]

  constructor(message: string, code = 'NETWORK_ERROR', status = 0, requestId: string | null = null, details: ValidationIssue[] = []) {
    super(message)
    this.name = 'ApiError'
    this.code = code
    this.status = status
    this.requestId = requestId
    this.details = details
  }
}

const READ_TIMEOUT_MS = 10_000
const SUBMIT_TIMEOUT_MS = 120_000

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

export function asApiError(error: unknown): ApiError {
  return error instanceof ApiError ? error : new ApiError('无法连接后台，请确认 API 已启动。')
}

export function isUncertainSubmission(error: ApiError): boolean {
  // A lost response, interrupted request, or server failure cannot prove that no task was saved.
  return error.status === 0 || error.status >= 500 || error.code === 'INVALID_RESPONSE'
}

export async function request<T>(path: string, init: RequestInit = {}, timeoutMs = READ_TIMEOUT_MS, binary = false): Promise<T> {
  const controller = new AbortController()
  const externalSignal = init.signal
  let timedOut = false
  const abort = () => controller.abort()
  if (externalSignal?.aborted) controller.abort()
  else externalSignal?.addEventListener('abort', abort, { once: true })
  const timeout = window.setTimeout(() => {
    timedOut = true
    controller.abort()
  }, timeoutMs)
  try {
    const response = await fetch(`/api${path}`, {
      ...init,
      signal: controller.signal,
      credentials: 'same-origin',
      cache: 'no-store',
      headers: { Accept: 'application/json', ...init.headers },
    })
    const requestIdHeader = response.headers.get('X-Request-ID')
    if (binary && response.ok) {
      if (response.headers.get('Content-Type')?.split(';')[0] !== 'image/png') {
        throw new ApiError('后台没有返回有效的 PNG 图片，请重新读取。', 'INVALID_RESPONSE', response.status, requestIdHeader)
      }
      const blob = await response.blob()
      if (!blob.size) throw new ApiError('后台返回的图片为空，请重新读取。', 'INVALID_RESPONSE', response.status, requestIdHeader)
      return blob as T
    }
    let body: unknown
    try {
      body = await response.json()
    } catch (error) {
      if (controller.signal.aborted) throw error
      throw new ApiError(
        response.status >= 500 ? '后台暂时不可用，请确认 API 正在运行后重试。' : '后台响应格式无效，请检查接口与页面版本。',
        'INVALID_RESPONSE', response.status, requestIdHeader,
      )
    }
    const requestId = isRecord(body) && typeof body.requestId === 'string' ? body.requestId : requestIdHeader
    if (!response.ok) {
      const detail = isRecord(body) && isRecord(body.error) ? body.error : null
      const issues = Array.isArray(detail?.details) ? detail.details.filter((item): item is ValidationIssue => (
        isRecord(item) && typeof item.field === 'string' && typeof item.code === 'string'
      )) : []
      throw new ApiError(
        typeof detail?.message === 'string' ? detail.message : `接口请求失败（HTTP ${response.status}）。`,
        typeof detail?.code === 'string' ? detail.code : 'HTTP_ERROR', response.status, requestId, issues,
      )
    }
    if (!isRecord(body) || !('data' in body) || typeof body.requestId !== 'string') {
      throw new ApiError('后台响应格式无效，请检查接口与页面版本。', 'INVALID_RESPONSE', response.status, requestId)
    }
    return body.data as T
  } catch (error) {
    if (timedOut) throw new ApiError('请求超时，请确认后台状态后重试。', 'REQUEST_TIMEOUT')
    if (controller.signal.aborted) throw new ApiError('请求已中断。', 'REQUEST_ABORTED')
    throw asApiError(error)
  } finally {
    window.clearTimeout(timeout)
    externalSignal?.removeEventListener('abort', abort)
  }
}

export const api = {
  async visionModels(signal?: AbortSignal): Promise<VisionModelSpec[]> {
    return (await request<{ items: VisionModelSpec[] }>('/models?taskType=vision', { signal })).items
  },
  visionTasks(page = 1, signal?: AbortSignal): Promise<TaskPage<VisionTaskView>> {
    return request(`/tasks?page=${page}&pageSize=20&taskType=vision`, { signal })
  },
  async visionTask(taskId: string, signal?: AbortSignal): Promise<VisionTaskView> {
    const task = await request<VisionTaskView>(`/tasks/${encodeURIComponent(taskId)}`, { signal })
    if (task.taskType !== 'vision' || task.taskId !== taskId) {
      throw new ApiError('这个编号不属于图片任务，请切换到对应工作台或重新选择任务。', 'TASK_TYPE_MISMATCH', 422)
    }
    return task
  },
  async visionResult(taskId: string, signal?: AbortSignal): Promise<TaskResultView<DetectionResult>> {
    const result = await request<TaskResultView<DetectionResult>>(`/tasks/${encodeURIComponent(taskId)}/result`, { signal })
    if (result.taskId !== taskId || !Array.isArray(result.output?.detections)) {
      throw new ApiError('后台返回的检测结果与当前任务不匹配，请重新读取。', 'INVALID_RESPONSE', 200)
    }
    return result
  },
  visionCancel(taskId: string, signal?: AbortSignal): Promise<CancellationReceipt<VisionTaskView>> {
    return request(`/tasks/${encodeURIComponent(taskId)}/cancel`, { method: 'POST', signal })
  },
  submitVision(file: File, modelId: string, modelVersion: string, parameters: VisionParameters, idempotencyKey: string, signal?: AbortSignal): Promise<TaskReceipt<VisionTaskView>> {
    const body = new FormData()
    body.append('file', file)
    body.append('modelId', modelId)
    body.append('modelVersion', modelVersion)
    body.append('confidenceThreshold', String(parameters.confidenceThreshold))
    body.append('iouThreshold', String(parameters.iouThreshold))
    body.append('maxDetections', String(parameters.maxDetections))
    return request('/tasks/vision', { method: 'POST', body, signal, headers: { 'Idempotency-Key': idempotencyKey } }, SUBMIT_TIMEOUT_MS)
  },
  image(taskId: string, variant: 'original' | 'annotated', signal?: AbortSignal): Promise<Blob> {
    return request(`/tasks/${encodeURIComponent(taskId)}/image?variant=${variant}`, { signal, headers: { Accept: 'image/png' } }, READ_TIMEOUT_MS, true)
  },
  async models(signal?: AbortSignal): Promise<ModelSpec[]> {
    return (await request<{ items: ModelSpec[] }>('/models', { signal })).items
  },
  runtime(signal?: AbortSignal): Promise<RuntimeView> {
    return request('/runtime', { signal })
  },
  tasks(page = 1, signal?: AbortSignal): Promise<TaskPage> {
    return request(`/tasks?page=${page}&pageSize=20`, { signal })
  },
  async task(taskId: string, signal?: AbortSignal): Promise<TaskView> {
    const task = await request<TaskView>(`/tasks/${encodeURIComponent(taskId)}`, { signal })
    if (task.taskType !== 'asr' || task.taskId !== taskId) {
      throw new ApiError('这个编号不属于音频任务，请切换到对应工作台或重新选择任务。', 'TASK_TYPE_MISMATCH', 422)
    }
    if (task.routing !== undefined && !validTaskRouting(task.routing)) {
      throw new ApiError('任务调度记录格式无效，请重新读取。', 'INVALID_RESPONSE', 200)
    }
    return task
  },
  async result(taskId: string, signal?: AbortSignal): Promise<TaskResultView> {
    const result = await request<TaskResultView>(`/tasks/${encodeURIComponent(taskId)}/result`, { signal })
    if (result.taskId !== taskId || typeof result.output?.text !== 'string') {
      throw new ApiError('后台返回的转写结果与当前任务不匹配，请重新读取。', 'INVALID_RESPONSE', 200)
    }
    return result
  },
  cancel(taskId: string, signal?: AbortSignal): Promise<CancellationReceipt> {
    return request(`/tasks/${encodeURIComponent(taskId)}/cancel`, { method: 'POST', signal })
  },
  submit(file: File, modelId: string, modelVersion: string, language: string | null, idempotencyKey: string, signal?: AbortSignal): Promise<TaskReceipt> {
    const body = new FormData()
    body.append('file', file)
    body.append('modelId', modelId)
    body.append('modelVersion', modelVersion)
    if (language !== null) body.append('language', language)
    return request('/tasks/asr', {
      method: 'POST', body, signal, headers: { 'Idempotency-Key': idempotencyKey },
    }, SUBMIT_TIMEOUT_MS)
  },
}

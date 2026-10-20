import { ApiError, asApiError, request } from './client'
import type { LlmEvent, LlmInput, LlmModelSpec, LlmStatus, OnlineCancellation, OnlineRequestPage, OnlineRequestView } from '../types/llm'
import { readSse } from '../utils/sse'

const record = (value: unknown): value is Record<string, unknown> => typeof value === 'object' && value !== null && !Array.isArray(value)
const states = ['accepted', 'waiting_instance', 'streaming', 'completed', 'failed', 'cancelled']
function validRecord(value: unknown, id: string): value is OnlineRequestView {
  return record(value) && value.onlineRequestId === id && typeof value.modelId === 'string' && typeof value.modelVersion === 'string'
    && states.includes(String(value.state)) && Number.isInteger(value.outputCharacters) && Number(value.outputCharacters) >= 0
}
function invalid(requestId: string | null = null) { return new ApiError('在线响应格式或顺序无效，生成已中断；请核对请求状态，不要自动重发。', 'INVALID_STREAM', 0, requestId) }

export const llmApi = {
  status(signal?: AbortSignal): Promise<LlmStatus> { return request('/llm/status', { signal }) },
  async models(signal?: AbortSignal): Promise<LlmModelSpec[]> {
    return (await request<{ items: LlmModelSpec[] }>('/models?taskType=llm', { signal })).items
  },
  list(page = 1, signal?: AbortSignal): Promise<OnlineRequestPage> { return request(`/llm/requests?page=${page}&pageSize=20`, { signal }) },
  async get(id: string, signal?: AbortSignal): Promise<OnlineRequestView> {
    const response = await request<OnlineRequestView>(`/llm/requests/${encodeURIComponent(id)}`, { signal })
    if (!validRecord(response, id)) throw invalid()
    return response
  },
  async cancel(id: string, signal?: AbortSignal): Promise<OnlineCancellation> {
    const response = await request<OnlineCancellation>(`/llm/requests/${encodeURIComponent(id)}/cancel`, { method: 'POST', signal, keepalive: true })
    if (!validRecord(response.request, id) || !['cancel_requested', 'already_finished'].includes(response.decision)) throw invalid()
    return response
  },
  async stream(input: LlmInput, signal: AbortSignal, onEvent: (event: LlmEvent) => void): Promise<void> {
    let requestId: string | null = null
    let accepted = false, terminal = false, sequence = 0, outputSize = 0
    try {
      const response = await fetch('/api/llm/requests', {
        method: 'POST', signal, credentials: 'same-origin', cache: 'no-store',
        headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' }, body: JSON.stringify(input),
      })
      requestId = response.headers.get('X-Request-ID')
      if (!response.ok) {
        let body: unknown
        try { body = await response.json() } catch { throw new ApiError(`在线请求失败（HTTP ${response.status}），请核对后台状态。`, 'INVALID_RESPONSE', response.status, requestId) }
        const error = record(body) && record(body.error) ? body.error : null
        throw new ApiError(typeof error?.message === 'string' ? error.message : '在线请求被后台拒绝。', typeof error?.code === 'string' ? error.code : 'HTTP_ERROR', response.status,
          record(body) && typeof body.requestId === 'string' ? body.requestId : requestId)
      }
      if (response.headers.get('Content-Type')?.split(';')[0].trim() !== 'text/event-stream' || !response.body) throw invalid(requestId)
      await readSse(response.body, signal, (name, data) => {
        let value: unknown
        try { value = JSON.parse(data) } catch { throw invalid(requestId) }
        if (!record(value) || value.type !== name || !['accepted', 'delta', 'heartbeat', 'done', 'error'].includes(name)
          || value.onlineRequestId !== input.onlineRequestId || value.sequence !== sequence + 1
          || typeof value.requestId !== 'string' || !value.requestId) throw invalid(requestId)
        if (requestId && value.requestId !== requestId) throw invalid(requestId)
        requestId = value.requestId
        if (value.record !== undefined && !validRecord(value.record, input.onlineRequestId)) throw invalid(requestId)
        if (name === 'accepted') {
          if (accepted || sequence !== 0 || !value.record) throw invalid(requestId)
          accepted = true
        } else if (!accepted) throw invalid(requestId)
        if (name === 'delta') {
          if (typeof value.text !== 'string' || !value.text) throw invalid(requestId)
          outputSize += value.text.length
          if (outputSize > 2_000_000) throw new ApiError('已接收文字超过页面安全上限，生成已中断。', 'OUTPUT_TOO_LARGE', 0, requestId)
        }
        if (name === 'done') {
          if (!validRecord(value.record, input.onlineRequestId) || !['completed', 'cancelled'].includes(value.record.state)) throw invalid(requestId)
          terminal = true
        }
        if (name === 'error') {
          if (!record(value.error) || typeof value.error.code !== 'string' || typeof value.error.message !== 'string') throw invalid(requestId)
          terminal = true
        }
        sequence += 1
        onEvent(value as unknown as LlmEvent)
        return !terminal
      })
      if (signal.aborted) throw new ApiError('在线连接已中止，请核对后台最终状态。', 'REQUEST_ABORTED', 0, requestId)
      if (!terminal) throw new ApiError('在线连接已断开，未收到最终事件；已收到的文字可能不完整。', 'STREAM_INTERRUPTED', 0, requestId)
    } catch (error) {
      if (signal.aborted) throw new ApiError('在线连接已中止，请核对后台最终状态。', 'REQUEST_ABORTED', 0, requestId)
      if (error instanceof ApiError) throw error
      if (accepted) throw new ApiError('流读取中断，已收到的文字可能不完整。请核对请求状态。', 'STREAM_INTERRUPTED', 0, requestId)
      throw asApiError(error)
    }
  },
}

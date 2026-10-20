/** Test substitutes only. No model service or persistent database is used. */
import type { LlmEvent, LlmInput, LlmModelSpec, LlmStatus, OnlineRequestView } from '../types/llm'
export const ONLINE_ID = 'a'.repeat(32)
export const llmModel: LlmModelSpec = {
  modelId: 'test-text-model', modelVersion: 'fixture-v1', modelRepo: 'fixture', taskType: 'llm', executionMode: 'online', adapterKey: 'openai-chat-completions',
  parameterSchema: { defaultTemperature: 0.7, defaultMaxTokens: 512, maxTokens: 2048 },
  inputConstraints: { maxMessages: 32, maxInputCharacters: 16000 }, outputCapabilities: ['text', 'stream'],
}
export const llmStatus: LlmStatus = { configured: true, availability: 'configured', message: '接口测试配置，非真实模型服务', maxConcurrentRequests: 1, activeRequests: 0, uncertainPermits: 0, serviceVerified: false }
export const llmInput: LlmInput = { onlineRequestId: ONLINE_ID, modelId: llmModel.modelId, modelVersion: llmModel.modelVersion, messages: [{ role: 'user', content: '测试输入' }], parameters: { temperature: 0.7, maxTokens: 512 } }
export function onlineRecord(overrides: Partial<OnlineRequestView> = {}): OnlineRequestView {
  return { onlineRequestId: ONLINE_ID, callerId: 'local-user', modelId: llmModel.modelId, modelVersion: llmModel.modelVersion, instanceId: null,
    state: 'accepted', permitState: 'held', acceptedAt: '2026-10-13T06:00:00Z', firstTokenAt: null, endedAt: null, deadlineAt: '2026-10-13T06:02:00Z',
    outputCharacters: 0, finishReason: null, error: null, cancelRequested: false, cancellationConfirmed: false, ...overrides }
}
export function llmEvent(type: LlmEvent['type'], sequence: number, extra: Partial<LlmEvent> = {}): LlmEvent {
  return { type, onlineRequestId: ONLINE_ID, requestId: 'http-request', sequence, ...extra }
}
export function deferred<T>() { let resolve!: (value: T) => void; let reject!: (reason: unknown) => void; const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no }); return { promise, resolve, reject } }

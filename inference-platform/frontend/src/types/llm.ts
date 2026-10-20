import type { FailureInfo, ModelSpec } from './api'

export interface LlmStatus {
  configured: boolean
  availability: 'unconfigured' | 'configured'
  message: string
  maxConcurrentRequests: number
  activeRequests: number
  uncertainPermits: number
  serviceVerified: false
}

export interface LlmModelSpec extends Omit<ModelSpec, 'taskType' | 'executionMode' | 'parameterSchema' | 'inputConstraints'> {
  taskType: 'llm'
  executionMode: 'online'
  parameterSchema: { defaultTemperature: number; defaultMaxTokens: number; maxTokens: number }
  inputConstraints: { maxMessages: number; maxInputCharacters: number }
}

export type OnlineState = 'accepted' | 'waiting_instance' | 'streaming' | 'completed' | 'failed' | 'cancelled'
export interface OnlineRequestView {
  onlineRequestId: string
  callerId: string
  modelId: string
  modelVersion: string
  instanceId: string | null
  state: OnlineState
  acceptedAt: string
  firstTokenAt: string | null
  endedAt: string | null
  deadlineAt: string
  outputCharacters: number
  finishReason: string | null
  error: FailureInfo | null
  cancelRequested: boolean
  cancellationConfirmed: boolean
  permitState: 'waiting' | 'held' | 'released' | 'uncertain'
}
export interface OnlineRequestPage { items: OnlineRequestView[]; total: number; page: number; pageSize: number }
export interface LlmInput {
  onlineRequestId: string
  modelId: string
  modelVersion: string
  messages: { role: 'user'; content: string }[]
  parameters: { temperature: number; maxTokens: number }
}
export interface LlmEvent {
  type: 'accepted' | 'delta' | 'heartbeat' | 'done' | 'error'
  onlineRequestId: string
  requestId: string
  sequence: number
  record?: OnlineRequestView
  text?: string
  error?: FailureInfo
}
export interface OnlineCancellation { request: OnlineRequestView; decision: 'cancel_requested' | 'already_finished' }
export const onlineFinished = (record: OnlineRequestView) => ['completed', 'failed', 'cancelled'].includes(record.state)

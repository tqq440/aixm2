import { ApiError, request } from './client'
import type { AsrServiceConfig, AsrServicesState, AsrServiceTestResult, AsrServiceView } from '@/types/asrServices'

const record = (value: unknown): value is Record<string, unknown> => value !== null && typeof value === 'object' && !Array.isArray(value)
const nullableString = (value: unknown) => value === null || typeof value === 'string'
const invalid = () => new ApiError('服务配置响应格式无效，请刷新后核对。', 'INVALID_RESPONSE')

function config(value: unknown): value is AsrServiceConfig {
  return record(value) && ['serviceId', 'name', 'baseUrl', 'transcribePath', 'healthPath', 'modelId'].every(key => typeof value[key] === 'string')
    && ['remote-v2', 'openai-audio'].includes(String(value.protocol))
    && ['modelVersion', 'upstreamModel', 'apiKeyEnv', 'caFile', 'insecureTlsOrigin'].every(key => nullableString(value[key]))
    && ['connectTimeoutSeconds', 'totalTimeoutSeconds'].every(key => typeof value[key] === 'number' && Number.isFinite(value[key]) && Number(value[key]) > 0)
}

function state(value: unknown): AsrServicesState {
  if (!record(value) || !Array.isArray(value.items) || !value.items.every((item): item is AsrServiceView => config(item) && typeof (item as AsrServiceView).revision === 'string')
    || new Set(value.items.map(item => item.serviceId)).size !== value.items.length
    || !['selectedServiceId', 'selectedRevision', 'runningServiceId', 'runningRevision'].every(key => nullableString(value[key]))
    || typeof value.restartRequired !== 'boolean'
    || !(value.legacyConfiguration === null || config(value.legacyConfiguration))
    || !(value.runningModel === null || (record(value.runningModel) && typeof value.runningModel.modelId === 'string' && typeof value.runningModel.modelVersion === 'string'))) throw invalid()
  return value as unknown as AsrServicesState
}

export const asrServicesApi = {
  async list(signal?: AbortSignal) { return state(await request<unknown>('/asr/services', { signal })) },
  async save(value: AsrServiceConfig, expectedRevision: string | null) {
    return state(await request<unknown>(`/asr/services/${encodeURIComponent(value.serviceId)}`, {
      method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ config: value, expectedRevision }),
    }))
  },
  async remove(serviceId: string, expectedRevision: string) {
    return state(await request<unknown>(`/asr/services/${encodeURIComponent(serviceId)}`, {
      method: 'DELETE', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ expectedRevision }),
    }))
  },
  async select(serviceId: string | null, expectedRevision: string | null) {
    return state(await request<unknown>('/asr/services/selection', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ serviceId, expectedRevision }),
    }))
  },
  async test(value: AsrServiceConfig): Promise<AsrServiceTestResult> {
    const result = await request<unknown>('/asr/services/test', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ config: value }),
    }, 35_000)
    if (!record(result) || typeof result.success !== 'boolean' || result.phase !== 'health'
      || typeof result.message !== 'string' || typeof result.configRevision !== 'string'
      || typeof result.durationMs !== 'number' || !Number.isFinite(result.durationMs) || result.durationMs < 0
      || !nullableString(result.errorCode) || !nullableString(result.apiVersion)) throw invalid()
    return result as unknown as AsrServiceTestResult
  },
}

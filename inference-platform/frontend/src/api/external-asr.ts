import { ApiError, request } from './client'
import type { ExternalAsrStatus } from '../types/api'
import { validAsrCapacity } from './asr-capacity'

const states = ['unconfigured', 'unavailable', 'ready', 'running', 'uncertain', 'stopping']

export async function readExternalAsrStatus(signal?: AbortSignal): Promise<ExternalAsrStatus> {
  const value = await request<ExternalAsrStatus>('/asr/external/status', { signal })
  if (!value || typeof value.configured !== 'boolean' || typeof value.available !== 'boolean'
    || !states.includes(value.state) || typeof value.message !== 'string'
    || !['currentTaskId', 'uncertainTaskId'].every(key => {
      const id = value[key as 'currentTaskId' | 'uncertainTaskId']
      return id === null || (typeof id === 'string' && /^[a-f0-9]{32}$/.test(id))
    }) || !['modelId', 'modelVersion'].every(key => value[key as 'modelId' | 'modelVersion'] === null || typeof value[key as 'modelId' | 'modelVersion'] === 'string')
    || (value.available && (!value.configured || !['ready', 'running'].includes(value.state) || !value.modelId || !value.modelVersion))
    || (value.versionInfo !== undefined && (!value.versionInfo || value.versionInfo.versionBasis !== 'integration_revision' || value.versionInfo.upstreamVersion !== null || !value.modelId || !value.modelVersion))
    || (value.observedAt !== null && (typeof value.observedAt !== 'string' || !Number.isFinite(Date.parse(value.observedAt))))) {
    throw new ApiError('外部服务状态格式无效，请重新读取。', 'INVALID_RESPONSE')
  }
  if (value.capacity != null && !validAsrCapacity(value.capacity)) {
    throw new ApiError('调用容量状态格式无效，请重新读取。', 'INVALID_RESPONSE')
  }
  return value
}

import { ApiError, request } from '@/api/client'

export type ValidationStage = 'module6a' | 'module6b'
export type DeferralCode = 'CAMPUS_NETWORK_UNAVAILABLE' | 'REDIS_CLIENT_MISSING' | 'REDIS_SERVER_UNVERIFIED' | 'LINUX_RUNTIME_UNVERIFIED'
export interface EnvironmentIssue {
  stage: ValidationStage; target: string; code: DeferralCode; source: 'platform' | 'operator'
  state: 'waiting_external_verification'; firstObservedAt: string; lastObservedAt: string
  observationCount: number; evidenceSha256: string | null
}
export interface EnvironmentIssuePage {
  items: EnvironmentIssue[]; total: number; offset: number; limit: number
  unreadableCount: number; scanTruncated: boolean; notice: string
}
const codes = new Set(['CAMPUS_NETWORK_UNAVAILABLE', 'REDIS_CLIENT_MISSING', 'REDIS_SERVER_UNVERIFIED', 'LINUX_RUNTIME_UNVERIFIED'])
const object = (value: unknown): value is Record<string, unknown> => typeof value === 'object' && value !== null && !Array.isArray(value)
const integer = (value: unknown): value is number => typeof value === 'number' && Number.isSafeInteger(value) && value >= 0
const date = (value: unknown): value is string => typeof value === 'string' && Number.isFinite(Date.parse(value))
function validIssue(value: unknown): value is EnvironmentIssue {
  return object(value) && (value.stage === 'module6a' || value.stage === 'module6b')
    && typeof value.target === 'string' && /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/.test(value.target)
    && typeof value.code === 'string' && codes.has(value.code)
    && (value.source === 'platform' || value.source === 'operator') && value.state === 'waiting_external_verification'
    && date(value.firstObservedAt) && date(value.lastObservedAt) && integer(value.observationCount) && value.observationCount >= 1
    && (value.evidenceSha256 === null || typeof value.evidenceSha256 === 'string' && /^[a-f0-9]{64}$/.test(value.evidenceSha256))
}
export const validationIssuesApi = {
  async list(stage: ValidationStage | '', offset = 0, signal?: AbortSignal): Promise<EnvironmentIssuePage> {
    const params = new URLSearchParams({ offset: String(offset), limit: '50' })
    if (stage) params.set('stage', stage)
    const value = await request<unknown>(`/admin/validation/issues?${params}`, { signal })
    if (!object(value) || !Array.isArray(value.items) || value.items.length > 50 || !value.items.every(validIssue)
      || !integer(value.total) || value.total < value.items.length || value.offset !== offset || value.limit !== 50
      || !integer(value.unreadableCount) || typeof value.scanTruncated !== 'boolean' || typeof value.notice !== 'string'
      || stage && value.items.some(item => item.stage !== stage)) {
      throw new ApiError('验收记录格式无效，尚不能确认当前状态。', 'INVALID_RESPONSE')
    }
    return value as unknown as EnvironmentIssuePage
  },
}

import { ApiError, request } from '@/api/client'
import type { AsrCatalogPage, AsrSchedulingStatus } from '@/types/asrScheduling'
import { validCatalogPage, validScheduling } from '@/utils/asrScheduling'

export async function readAsrScheduling(signal?: AbortSignal): Promise<AsrSchedulingStatus> {
  const value = await request<unknown>('/asr/external/scheduling', { signal })
  if (!validScheduling(value)) throw new ApiError('调度状态格式无效，请重新读取。', 'INVALID_RESPONSE')
  return value
}

export async function readAsrTargets(offset = 0, limit = 5, signal?: AbortSignal): Promise<AsrCatalogPage> {
  if (!Number.isSafeInteger(offset) || offset < 0 || !Number.isSafeInteger(limit) || limit < 1 || limit > 100) {
    throw new ApiError('目标目录页码无效。', 'INVALID_QUERY')
  }
  const value = await request<unknown>(`/asr/external/targets?offset=${offset}&limit=${limit}`, { signal })
  if (!validCatalogPage(value) || value.offset !== offset || value.limit !== limit) {
    throw new ApiError('目标目录格式或页码不匹配，请重新读取。', 'INVALID_RESPONSE')
  }
  return value
}

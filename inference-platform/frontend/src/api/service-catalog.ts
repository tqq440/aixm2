import { ApiError, request } from '@/api/client'
import type { CatalogConfig, CatalogEntry, CatalogGroup, CatalogGroups, CatalogPage } from '@/types/serviceCatalog'

const root = '/admin/service-catalog'
const json = (body: unknown) => ({ headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
const servicePath = (id: string) => `${root}/services/${encodeURIComponent(id)}`

export const serviceCatalogApi = {
  async list(page = 1): Promise<CatalogPage> {
    const value = await request<CatalogPage>(`${root}/services?page=${page}`)
    if (!value || !Array.isArray(value.items) || !Number.isInteger(value.total) || value.module !== 'catalog-only') {
      throw new ApiError('目录响应格式无效，请核对前后端版本。', 'INVALID_RESPONSE')
    }
    return value
  },
  groups: (page = 1) => request<CatalogGroups>(`${root}/resource-groups?page=${page}&pageSize=100`),
  get: (id: string) => request<CatalogEntry>(servicePath(id)),
  save: (config: CatalogConfig, expectedVersion: number | null) => request<CatalogEntry>(servicePath(config.serviceId), {
    method: 'PUT', ...json({ config, expectedVersion }),
  }),
  activate: (entry: CatalogEntry, enabled: boolean) => request<CatalogEntry>(`${servicePath(entry.serviceId)}/activation`, {
    method: 'POST', ...json({ expectedVersion: entry.version, revision: entry.draftRevision, enabled }),
  }),
  test: (entry: CatalogEntry, targetId: string) => request<{ success: boolean; message: string; errorCode: string | null }>(
    `${servicePath(entry.serviceId)}/targets/${encodeURIComponent(targetId)}/test`, {
      method: 'POST', ...json({ expectedVersion: entry.version, revision: entry.draftRevision }),
    }),
  saveGroup: (config: CatalogGroup, expectedVersion: number | null) => request(`${root}/resource-groups/${encodeURIComponent(config.groupId)}`, {
    method: 'PUT', ...json({ config, expectedVersion }),
  }),
  importAsr: () => request<{ imported: string[]; skipped: string[]; message: string }>(`${root}/import-asr`, { method: 'POST', ...json({}) }),
}

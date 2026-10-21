import type { CatalogEntry, CatalogGroups, CatalogPage } from '@/types/serviceCatalog'

export const catalogEntry = (): CatalogEntry => ({
  serviceId: 'speech-main', version: 1, draftRevision: 'a'.repeat(64), activeRevision: null, enabled: false,
  activeConfig: null, executionReady: false, source: 'manual', createdAt: '2026-10-20T09:00:00Z', updatedAt: '2026-10-20T09:00:00Z', checks: [],
  config: { serviceId: 'speech-main', name: '示例语音服务', kind: 'asr', modelId: 'sensevoice', modelVersion: null,
    protocol: 'remote-v2', invocationMode: 'sync', policy: 'fixed', maxFileBytes: 20 * 1024 * 1024, maxAudioSeconds: 300,
    targets: [{ targetId: 'primary', name: '主要目标', enabled: true, baseUrl: 'http://127.0.0.1:8010',
      invokePath: '/api/v1/asr/transcripts', healthPath: '/api/v1/asr/health', upstreamModel: null, apiKeyEnv: null,
      caFile: null, insecureTlsOrigin: null, connectTimeoutSeconds: 5, totalTimeoutSeconds: 600, concurrencyLimit: 1,
      capacityEvidence: null, resourceGroupIds: [], deviceKind: 'unknown', deviceId: null, deviceEvidence: null,
      compatibilityEvidence: null, monitoring: { protocol: 'none', statusPath: null, resultPath: null,
        reportsProgress: false, reportsDevice: false, pollSeconds: 5 } }] },
  issues: [{ code: 'EXECUTION_NOT_CONNECTED', targetId: null, message: '公共执行层尚未接入；本目录不会用于当前任务。', blocksActivation: false },
    { code: 'PROGRESS_UNSUPPORTED', targetId: 'primary', message: '服务未提供进度；不推算百分比。', blocksActivation: false }],
})
export const catalogPage = (items = [catalogEntry()]): CatalogPage => ({ items, total: items.length, page: 1, pageSize: 20, module: 'catalog-only', executionConnected: false })
export const catalogGroups = (): CatalogGroups => ({ items: [], total: 0, page: 1, pageSize: 100 })

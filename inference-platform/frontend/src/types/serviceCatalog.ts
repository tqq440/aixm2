export type ServiceKind = 'asr' | 'vision' | 'llm'
export type ServiceProtocol = 'remote-v2' | 'openai-audio' | 'vision-json-v1' | 'chat-completions-sse'
export interface MonitoringConfig {
  protocol: 'none' | 'job-json-v1'; statusPath: string | null; resultPath: string | null
  reportsProgress: boolean; reportsDevice: boolean; pollSeconds: number
  resultInStatus?: boolean
}
export interface CatalogTarget {
  targetId: string; name: string; enabled: boolean; baseUrl: string; invokePath: string; healthPath: string
  upstreamModel: string | null; apiKeyEnv: string | null; caFile: string | null; insecureTlsOrigin: string | null
  connectTimeoutSeconds: number; totalTimeoutSeconds: number; concurrencyLimit: number; capacityEvidence: string | null; resourceGroupIds: string[]
  deviceKind: 'unknown' | 'cpu' | 'gpu'; deviceId: string | null; deviceEvidence: string | null
  compatibilityEvidence: string | null; monitoring: MonitoringConfig
  node?: { nodeId: string; serviceId: string; configRevision: string; baseUrl: string; executorTokenEnv: string } | null
}
export interface CatalogConfig {
  serviceId: string; name: string; kind: ServiceKind; modelId: string; modelVersion: string | null
  protocol: ServiceProtocol; invocationMode: 'sync' | 'async' | 'stream'; policy: 'fixed' | 'gpu_preferred'
  maxFileBytes: number; maxAudioSeconds: number; targets: CatalogTarget[]
}
export interface CatalogEntry {
  serviceId: string; version: number; draftRevision: string; activeRevision: string | null; enabled: boolean
  config: CatalogConfig; activeConfig: CatalogConfig | null; executionReady: boolean; source: string
  createdAt: string; updatedAt: string
  issues: { code: string; targetId: string | null; message: string; blocksActivation: boolean }[]
  checks: { revision: string; targetId: string; success: boolean; errorCode: string | null; durationMs: number; checkedAt: string }[]
}
export interface CatalogPage { items: CatalogEntry[]; total: number; page: number; pageSize: number; module: string; executionConnected: boolean }
export interface CatalogGroup { groupId: string; name: string; concurrencyLimit: number; capacityEvidence: string | null }
export interface CatalogGroupView extends CatalogGroup { version: number; updatedAt: string }
export interface CatalogGroups { items: CatalogGroupView[]; total: number; page: number; pageSize: number }

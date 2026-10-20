export type DeviceKind = 'unknown' | 'gpu' | 'cpu'
export type EvidenceKind = 'provided' | 'isolated'

export interface AsrSchedulingStatus {
  observedAt: string
  mode: 'legacy' | 'isolated' | 'blocked'
  routingConfigured: boolean
  executionScope: 'legacy' | 'isolated' | 'disabled'
  reasonCode: string
  candidateCount: number
  message: string
}

export interface AsrResourceObservation {
  observationId: string
  scope: 'target' | 'capacity_group'
  source: string
  evidenceKind: EvidenceKind
  observedAt: string
  recordedAt: string
  expiresAt: string
  freshness: 'unknown' | 'fresh' | 'stale'
  reportedHealth: 'unknown' | 'ready' | 'unavailable'
  reportedAvailableConcurrency: number | null
  gpuUtilizationPercent: number | null
  gpuMemoryFreeMiB: number | null
  cpuAvailableCores: number | null
  usableForScheduling: false
}

export interface AsrCatalogTarget {
  targetId: string
  capacityGroupId: string
  targetLimit: number
  groupLimit: number
  legacyEnabled: boolean
  mode: 'legacy' | 'catalog_only'
  connectionRevisionId: string | null
  catalogRevisionId: string | null
  deviceKind: DeviceKind
  verificationState: 'unverified'
  modelId: string | null
  modelVersion: string | null
  hasDeviceEvidence: boolean
  hasCompatibilityEvidence: boolean
  observations: AsrResourceObservation[]
  routingEnabled: false
}

export interface AsrCatalogPage {
  observedAt: string
  total: number
  offset: number
  limit: number
  items: AsrCatalogTarget[]
  routingEnabled: false
}

export interface TaskRoutingView {
  routingRevisionId: string
  selectionState: 'waiting' | 'selected'
  catalogRevisionId: string | null
  targetId: string | null
  deviceKind: 'gpu' | 'cpu' | null
  reasonCode: string | null
  evidenceKind: EvidenceKind | null
  selectedAt: string | null
  observedAt?: string | null
  message?: string | null
  reasonCategory?: 'selected' | 'waiting' | 'blocked' | 'outcome_unknown' | 'terminal' | null
  statusCode?: string | null
  candidates?: { targetId: string; deviceKind: DeviceKind; eligible: boolean; reasonCode: string; message: string }[]
}

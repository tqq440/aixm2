import type { AsrCatalogPage, AsrResourceObservation, AsrSchedulingStatus, TaskRoutingView } from '@/types/asrScheduling'

const record = (value: unknown): value is Record<string, unknown> => typeof value === 'object' && value !== null && !Array.isArray(value)
const text = (value: unknown): value is string => typeof value === 'string' && value.length > 0 && value.length <= 2048
const id = (value: unknown): value is string => typeof value === 'string' && /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/.test(value)
const count = (value: unknown, max = Number.MAX_SAFE_INTEGER): value is number => Number.isSafeInteger(value) && (value as number) >= 0 && (value as number) <= max
const member = (value: unknown, values: readonly string[]) => typeof value === 'string' && values.includes(value)
const nullable = (value: unknown, valid: (item: unknown) => boolean) => value === null || valid(value)
const optional = (value: unknown, valid: (item: unknown) => boolean) => value === undefined || nullable(value, valid)
const number = (value: unknown, max = Number.MAX_VALUE) => typeof value === 'number' && Number.isFinite(value) && value >= 0 && value <= max
export const validTime = (value: unknown): value is string => typeof value === 'string' && /(?:Z|[+-]\d{2}:\d{2})$/.test(value) && Number.isFinite(Date.parse(value))
export const snapshotFresh = (observedAt: string | null | undefined, now = Date.now()) => {
  const age = observedAt ? now - Date.parse(observedAt) : Infinity
  return age >= -5_000 && age < 10_000
}

export function observationFreshness(value: AsrResourceObservation, trusted: boolean, now = Date.now()): 'fresh' | 'stale' | 'unknown' {
  if (!trusted || value.freshness === 'unknown' || now < Math.max(Date.parse(value.observedAt), Date.parse(value.recordedAt))) return 'unknown'
  return value.freshness === 'stale' || now >= Date.parse(value.expiresAt) ? 'stale' : 'fresh'
}

export function validScheduling(value: unknown): value is AsrSchedulingStatus {
  if (!record(value) || !validTime(value.observedAt) || !member(value.mode, ['legacy', 'isolated', 'blocked'])
    || typeof value.routingConfigured !== 'boolean' || !id(value.reasonCode) || !count(value.candidateCount, 8) || !text(value.message)) return false
  return (value.mode === 'legacy' && !value.routingConfigured && value.executionScope === 'legacy' && value.candidateCount === 0)
    || (value.mode === 'isolated' && value.routingConfigured && value.executionScope === 'isolated')
    || (value.mode === 'blocked' && value.executionScope === 'disabled')
}

function validObservation(value: unknown): boolean {
  return record(value) && id(value.observationId) && member(value.scope, ['target', 'capacity_group'])
    && id(value.source) && member(value.evidenceKind, ['provided', 'isolated'])
    && [value.observedAt, value.recordedAt, value.expiresAt].every(validTime)
    && Date.parse(value.expiresAt as string) > Date.parse(value.observedAt as string)
    && member(value.freshness, ['unknown', 'fresh', 'stale']) && member(value.reportedHealth, ['unknown', 'ready', 'unavailable'])
    && nullable(value.reportedAvailableConcurrency, item => count(item, 1024))
    && nullable(value.gpuUtilizationPercent, item => number(item, 100))
    && nullable(value.gpuMemoryFreeMiB, item => count(item)) && nullable(value.cpuAvailableCores, item => number(item))
    && value.usableForScheduling === false
}

export function validCatalogPage(value: unknown): value is AsrCatalogPage {
  if (!record(value) || !validTime(value.observedAt) || !count(value.total) || !count(value.offset)
    || !count(value.limit, 100) || value.limit === 0 || value.routingEnabled !== false
    || !Array.isArray(value.items) || value.items.length > value.limit || value.items.length > Math.max(0, value.total - value.offset)) return false
  const targetIds = new Set<string>()
  return value.items.every(item => {
    if (!record(item) || !id(item.targetId) || targetIds.has(item.targetId) || !id(item.capacityGroupId)
      || !count(item.targetLimit, 8) || item.targetLimit === 0 || !count(item.groupLimit, 8) || item.groupLimit === 0
      || typeof item.legacyEnabled !== 'boolean' || !member(item.mode, ['legacy', 'catalog_only'])
      || !nullable(item.connectionRevisionId, id) || !nullable(item.catalogRevisionId, id)
      || !member(item.deviceKind, ['unknown', 'gpu', 'cpu']) || item.verificationState !== 'unverified'
      || !nullable(item.modelId, text) || !nullable(item.modelVersion, text)
      || typeof item.hasDeviceEvidence !== 'boolean' || typeof item.hasCompatibilityEvidence !== 'boolean'
      || item.routingEnabled !== false || !Array.isArray(item.observations) || item.observations.length > 2
      || !item.observations.every(validObservation)) return false
    targetIds.add(item.targetId)
    return new Set(item.observations.map(row => (row as AsrResourceObservation).scope)).size === item.observations.length
  })
}

export function validTaskRouting(value: unknown): value is TaskRoutingView {
  if (!record(value) || !id(value.routingRevisionId) || !member(value.selectionState, ['waiting', 'selected'])
    || !nullable(value.catalogRevisionId, id) || !nullable(value.targetId, id)
    || !nullable(value.deviceKind, item => member(item, ['gpu', 'cpu']))
    || !nullable(value.reasonCode, id) || !nullable(value.evidenceKind, item => member(item, ['isolated', 'provided']))
    || !nullable(value.selectedAt, validTime) || !optional(value.observedAt, validTime) || !optional(value.message, text)
    || !optional(value.statusCode, id) || !optional(value.reasonCategory, item => member(item, ['selected', 'waiting', 'blocked', 'outcome_unknown', 'terminal']))) return false
  if (value.selectionState === 'selected' && !(value.catalogRevisionId && value.targetId && value.deviceKind && value.selectedAt)) return false
  if (value.selectionState === 'waiting' && (value.catalogRevisionId !== null || value.targetId !== null || value.selectedAt !== null)) return false
  if (value.candidates === undefined) return true
  return Array.isArray(value.candidates) && value.candidates.length <= 8
    && value.candidates.every(item => record(item) && id(item.targetId) && member(item.deviceKind, ['unknown', 'gpu', 'cpu'])
      && typeof item.eligible === 'boolean' && id(item.reasonCode) && text(item.message))
    && new Set(value.candidates.map(item => item.targetId)).size === value.candidates.length
}

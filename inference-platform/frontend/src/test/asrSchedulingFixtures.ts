import type { AsrCatalogPage, AsrCatalogTarget, AsrResourceObservation, AsrSchedulingStatus, TaskRoutingView } from '../types/asrScheduling'

export function schedulingFixture(changes: Partial<AsrSchedulingStatus> = {}): AsrSchedulingStatus {
  return { observedAt: new Date().toISOString(), mode: 'legacy', routingConfigured: false, executionScope: 'legacy',
    reasonCode: 'ASR_ROUTING_LEGACY', candidateCount: 0, message: '沿用原固定调用目标，设备与真实容量待核实。', ...changes }
}

export function resourceFixture(changes: Partial<AsrResourceObservation> = {}): AsrResourceObservation {
  return { observationId: '1'.repeat(64), scope: 'target', source: 'isolated-fixture', evidenceKind: 'isolated',
    observedAt: new Date().toISOString(), recordedAt: new Date().toISOString(), expiresAt: new Date(Date.now() + 60_000).toISOString(),
    freshness: 'fresh', reportedHealth: 'ready', reportedAvailableConcurrency: 2,
    gpuUtilizationPercent: 10, gpuMemoryFreeMiB: 2048, cpuAvailableCores: null, usableForScheduling: false, ...changes }
}

export function targetFixture(changes: Partial<AsrCatalogTarget> = {}): AsrCatalogTarget {
  return { targetId: 'fixture-gpu', capacityGroupId: 'fixture-shared', targetLimit: 1, groupLimit: 1,
    legacyEnabled: false, mode: 'catalog_only', connectionRevisionId: '2'.repeat(64), catalogRevisionId: '3'.repeat(64),
    deviceKind: 'gpu', verificationState: 'unverified', modelId: 'fixture-asr', modelVersion: 'fixture-v1',
    hasDeviceEvidence: true, hasCompatibilityEvidence: true, observations: [resourceFixture()], routingEnabled: false, ...changes }
}

export function catalogFixture(changes: Partial<AsrCatalogPage> = {}): AsrCatalogPage {
  return { observedAt: new Date().toISOString(), total: 1, offset: 0, limit: 5, items: [targetFixture()], routingEnabled: false, ...changes }
}

export function taskRoutingFixture(changes: Partial<TaskRoutingView> = {}): TaskRoutingView {
  return { routingRevisionId: '4'.repeat(64), selectionState: 'waiting', catalogRevisionId: null, targetId: null,
    deviceKind: null, reasonCode: 'GPU_STATE_UNCONFIRMED', evidenceKind: 'isolated', selectedAt: null,
    observedAt: new Date().toISOString(), message: 'GPU资源报告尚未确认，继续等待，不改派CPU。',
    reasonCategory: 'waiting', statusCode: 'GPU_STATE_UNCONFIRMED', candidates: [{ targetId: 'fixture-gpu',
      deviceKind: 'gpu', eligible: false, reasonCode: 'CAPACITY_STALE', message: '资源报告已过期，请核对新的观测。' }], ...changes }
}

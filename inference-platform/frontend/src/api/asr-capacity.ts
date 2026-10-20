import type { ExternalAsrCapacity } from '../types/api'

const count = (value: unknown) => Number.isSafeInteger(value) && (value as number) >= 0
const optionalId = (value: unknown) => value === null || (typeof value === 'string' && /^[a-f0-9]{32}$/.test(value))
const time = (value: unknown) => typeof value === 'string' && Number.isFinite(Date.parse(value))

export function validAsrCapacity(value: ExternalAsrCapacity): boolean {
  if (!value || !time(value.observedAt) || typeof value.targetId !== 'string'
    || typeof value.registered !== 'boolean' || typeof value.enabled !== 'boolean'
    || !['unknown', 'cpu', 'gpu'].includes(value.deviceKind) || typeof value.poolState !== 'string'
    || !(value.revisionId === null || typeof value.revisionId === 'string')
    || !(value.capacityGroup === null || typeof value.capacityGroup === 'string')
    || ![value.targetLimit, value.groupLimit, value.held, value.uncertain, value.groupHeld,
      value.groupUncertain, value.remainingCapacity, value.queued, value.incompatibleQueued,
      value.legacyUnknownCount, value.activeCallsTotal].every(count)
    || value.remainingCapacity > Math.max(0, Math.min(value.targetLimit - value.held - value.uncertain,
      value.groupLimit - value.groupHeld - value.groupUncertain))
    || !Array.isArray(value.slots) || value.slots.length > 8 || !Array.isArray(value.activeCalls)
    || value.activeCalls.length > 50 || value.activeCalls.length > value.activeCallsTotal
    || typeof value.activeCallsTruncated !== 'boolean'
    || value.activeCallsTruncated !== (value.activeCalls.length < value.activeCallsTotal)) return false
  return new Set(value.slots.map(slot => slot?.slotIndex)).size === value.slots.length
    && value.slots.every(slot => slot && count(slot.slotIndex) && slot.slotIndex < value.targetLimit
      && typeof slot.state === 'string' && typeof slot.fresh === 'boolean' && typeof slot.modelReady === 'boolean'
      && optionalId(slot.sessionId) && optionalId(slot.currentTaskId)
      && (slot.lastError === null || typeof slot.lastError === 'string')
      && (slot.heartbeatAt === null || time(slot.heartbeatAt)))
    && value.activeCalls.every(call => call && call.taskId !== null && optionalId(call.taskId)
      && ['held', 'uncertain'].includes(call.state) && typeof call.targetId === 'string'
      && count(call.slotIndex) && typeof call.ownerFresh === 'boolean')
}

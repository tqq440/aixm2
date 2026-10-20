import type { ExternalAsrCapacity } from '../types/api'

export const capacityFixture = (): ExternalAsrCapacity => ({
  observedAt: new Date().toISOString(), targetId: 'remote-default', registered: true, enabled: true,
  revisionId: 'a'.repeat(64), capacityGroup: 'fixture-shared', deviceKind: 'unknown',
  targetLimit: 2, groupLimit: 2, held: 1, uncertain: 1, groupHeld: 1, groupUncertain: 1,
  remainingCapacity: 0, queued: 3, incompatibleQueued: 0, legacyUnknownCount: 0, poolState: 'running',
  slots: [
    { slotIndex: 0, state: 'running', fresh: true, modelReady: true, sessionId: '1'.repeat(32), currentTaskId: '3'.repeat(32), lastError: null, heartbeatAt: new Date().toISOString() },
    { slotIndex: 1, state: 'ready', fresh: true, modelReady: true, sessionId: '2'.repeat(32), currentTaskId: null, lastError: 'REMOTE_ASR_RESULT_UNKNOWN', heartbeatAt: new Date().toISOString() },
  ],
  activeCalls: [
    { taskId: '3'.repeat(32), state: 'held', targetId: 'remote-default', slotIndex: 0, ownerFresh: true },
    { taskId: '4'.repeat(32), state: 'uncertain', targetId: 'remote-default', slotIndex: 1, ownerFresh: true },
  ], activeCallsTotal: 2, activeCallsTruncated: false,
})

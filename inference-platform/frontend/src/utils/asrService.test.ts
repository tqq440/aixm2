import { describe, expect, it } from 'vitest'
import { asrSubmissionBlock } from './asrService'
import type { ManagementStatus } from '../types/management'

const ready: ManagementStatus = {
  instanceId: 'a'.repeat(32), nodeId: 'b'.repeat(32), managementMode: 'node_agent', state: 'ready',
  reportedState: 'ready', desiredState: 'running', agentAvailable: true, agentSessionId: 'c'.repeat(32),
  agentPid: 123, workerSessionId: 'd'.repeat(32), childPid: 456, commandRevision: 1,
  modelReady: true, acceptingTasks: true, activeTaskId: null, lastError: null,
  heartbeatAt: '2026-10-14T02:00:00Z', evaluatedAt: '2026-10-14T02:00:00Z', exitConfirmedAt: null, shutdownConfirmedAt: null,
}
describe('ASR submission display gate', () => {
  it.each(['ready', 'busy'] as const)('allows a loaded %s instance to accept queued work', state => {
    expect(asrSubmissionBlock({ ...ready, state }, false, null)).toBeNull()
  })
  it.each(['starting', 'stopped', 'stopping', 'stop_unconfirmed', 'unknown', 'failed'] as const)('blocks %s', state => {
    expect(asrSubmissionBlock({ ...ready, state }, false, null)).toBeTruthy()
  })
  it('does not call failed or unknown stopped-intent states draining', () => {
    expect(asrSubmissionBlock({ ...ready, state: 'failed', desiredState: 'stopped' }, false, null)).toContain('失败')
    expect(asrSubmissionBlock({ ...ready, state: 'unknown', desiredState: 'stopped' }, false, null)).toContain('尚未确认')
  })
  it('blocks lost reads, unavailable Agent, stop intent and pending stop immediately', () => {
    expect(asrSubmissionBlock(ready, true, null)).toBeTruthy()
    expect(asrSubmissionBlock(null, false, null)).toBeTruthy()
    expect(asrSubmissionBlock({ ...ready, agentAvailable: false }, false, null)).toBeTruthy()
    expect(asrSubmissionBlock({ ...ready, acceptingTasks: false }, false, null)).toBeTruthy()
    expect(asrSubmissionBlock({ ...ready, desiredState: 'stopped' }, false, null)).toContain('正在停止')
    expect(asrSubmissionBlock(ready, false, 'stop')).toContain('正在发送')
  })
})

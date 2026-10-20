/** UI lifecycle substitutes; actual model/Agent evidence belongs to the stage record. */
import { act, cleanup, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from '../api/client'
import { managementApi } from '../api/management'
import type { AsrInstance, ManagementStatus, RegisteredNode } from '../types/management'
import { useAsrManagement } from './useAsrManagement'

const NODE = 'a'.repeat(32), INSTANCE = 'b'.repeat(32), SESSION = 'c'.repeat(32)
const node: RegisteredNode = { nodeId: NODE, hostname: 'test-laptop', osName: 'Windows', osVersion: 'test', architecture: 'AMD64', cpuCount: 4, gpuStatus: 'not_probed', registrationState: 'registered', agentStatus: 'online', registeredAt: '2026-10-14T00:00:00Z', metadataObservedAt: '2026-10-14T00:00:00Z' }
const instance: AsrInstance = { instanceId: INSTANCE, nodeId: NODE, instanceKey: 'asr-default', modelId: 'whisper-small', modelVersion: 'registered-version', adapterKey: 'faster-whisper', executionDevice: 'cpu', registrationState: 'registered', runtimeState: 'unknown', managementMode: 'node_agent', registeredAt: '2026-10-14T00:00:00Z' }
const status = (fields: Partial<ManagementStatus> = {}): ManagementStatus => ({ instanceId: INSTANCE, nodeId: NODE, managementMode: 'node_agent', state: 'stopped', reportedState: 'stopped', desiredState: 'stopped', agentAvailable: true, agentSessionId: SESSION, agentPid: 10, workerSessionId: null, childPid: null, commandRevision: 4, modelReady: false, acceptingTasks: false, activeTaskId: null, lastError: null, heartbeatAt: '2026-10-14T00:00:00Z', exitConfirmedAt: null, shutdownConfirmedAt: null, evaluatedAt: '2026-10-14T00:00:00Z', ...fields })
const ready = () => status({ state: 'ready', desiredState: 'running', modelReady: true, acceptingTasks: true })
function deferred<T>() { let resolve!: (value: T) => void; const promise = new Promise<T>(accept => { resolve = accept }); return { promise, resolve } }
const settle = async () => { await act(async () => { await Promise.resolve() }) }
function visibility(value: 'hidden' | 'visible') { Object.defineProperty(document, 'visibilityState', { configurable: true, value }); document.dispatchEvent(new Event('visibilitychange')) }
beforeEach(() => {
  vi.useFakeTimers()
  Object.defineProperty(document, 'visibilityState', { configurable: true, value: 'visible' })
  vi.spyOn(managementApi, 'nodes').mockResolvedValue([node])
  vi.spyOn(managementApi, 'instances').mockResolvedValue([instance])
  vi.spyOn(managementApi, 'status').mockResolvedValue(status())
  vi.spyOn(managementApi, 'command').mockResolvedValue(status({ state: 'starting', desiredState: 'running' }))
})
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.useRealTimers() })

describe('ASR management browser lifecycle', () => {
  it('reads only while active and never treats registration as model readiness', async () => {
    const hook = renderHook(({ active }) => useAsrManagement(active), { initialProps: { active: false } })
    await settle(); expect(managementApi.nodes).not.toHaveBeenCalled()
    hook.rerender({ active: true }); await settle()
    expect(hook.result.current.selectedInstance).toEqual(instance)
    expect(hook.result.current.status?.state).toBe('stopped')
    expect(hook.result.current.status?.modelReady).toBe(false)
    expect(managementApi.command).not.toHaveBeenCalled()
  })

  it('polls every two seconds without starting a model and clears ready on a failed read', async () => {
    vi.mocked(managementApi.status).mockResolvedValueOnce(ready()).mockRejectedValue(new ApiError('断网', 'NETWORK_ERROR'))
    const hook = renderHook(() => useAsrManagement(true)); await settle()
    expect(hook.result.current.status?.state).toBe('ready')
    await act(async () => { await vi.advanceTimersByTimeAsync(2_000) })
    expect(hook.result.current.status).toBeNull()
    expect(hook.result.current.error?.code).toBe('NETWORK_ERROR')
    expect(managementApi.command).not.toHaveBeenCalled()
  })

  it('keeps empty registries empty and recovers when a real registration becomes available', async () => {
    vi.mocked(managementApi.nodes).mockResolvedValueOnce([])
    const hook = renderHook(() => useAsrManagement(true)); await settle()
    expect(hook.result.current.selectedNodeId).toBeNull()
    expect(hook.result.current.status).toBeNull()
    expect(managementApi.status).not.toHaveBeenCalled()
    act(() => hook.result.current.refresh()); await settle()
    expect(hook.result.current.selectedNodeId).toBe(NODE)
  })

  it('sends one explicit command with the observed session/revision, then waits for a fresh GET', async () => {
    const post = deferred<ManagementStatus>(), next = deferred<ManagementStatus>()
    vi.mocked(managementApi.command).mockReturnValue(post.promise)
    const hook = renderHook(() => useAsrManagement(true)); await settle()
    vi.mocked(managementApi.status).mockReturnValue(next.promise)
    act(() => { void hook.result.current.start('test-token'); void hook.result.current.start('test-token') })
    expect(hook.result.current.commandPending).toBe('start')
    expect(hook.result.current.status).toBeNull()
    expect(managementApi.command).toHaveBeenCalledTimes(1)
    expect(managementApi.command).toHaveBeenCalledWith(INSTANCE, 'start', { agentSessionId: SESSION, expectedRevision: 4 }, 'test-token', expect.any(AbortSignal))
    await act(async () => post.resolve(ready()))
    expect(hook.result.current.status).toBeNull()
    expect(hook.result.current.commandNotice).toContain('启动命令已受理')
    await act(async () => next.resolve(status({ state: 'starting' })))
    expect(hook.result.current.status?.state).toBe('starting')
  })

  it.each([
    [new ApiError('旧会话', 'AGENT_COMMAND_STALE', 409), '手动重试'],
    [new ApiError('断网', 'NETWORK_ERROR'), '命令结果尚未确认'],
    [new ApiError('错误', 'HTTP_ERROR', 503), '命令结果尚未确认'],
  ])('reconciles %s by GET, never by automatic POST retry', async (failure, note) => {
    vi.mocked(managementApi.command).mockRejectedValue(failure)
    const hook = renderHook(() => useAsrManagement(true)); await settle()
    await act(async () => hook.result.current.start('test-token'))
    await settle()
    expect(hook.result.current.commandError).toBe(failure)
    expect(hook.result.current.commandNotice).toContain(note)
    expect(managementApi.status).toHaveBeenCalledTimes(2)
    await act(async () => { await vi.advanceTimersByTimeAsync(8_000) })
    expect(managementApi.command).toHaveBeenCalledTimes(1)
  })

  it('does not confuse a stopped command receipt with process exit when GET still reports draining', async () => {
    vi.mocked(managementApi.status).mockResolvedValueOnce(ready()).mockResolvedValue(status({ state: 'stopping', modelReady: true, activeTaskId: 'd'.repeat(32) }))
    vi.mocked(managementApi.command).mockResolvedValue(status())
    const hook = renderHook(() => useAsrManagement(true)); await settle()
    await act(async () => hook.result.current.stop('test-token')); await settle()
    expect(hook.result.current.status?.state).toBe('stopping')
    expect(hook.result.current.status?.activeTaskId).toBe('d'.repeat(32))
    expect(hook.result.current.commandNotice).toContain('退出确认')
  })

  it.each([{ agentAvailable: false }, { agentSessionId: null }, { state: 'stop_unconfirmed' as const }, { state: 'unknown' as const }])('rejects management commands against unavailable or uncertain state %j', async fields => {
    vi.mocked(managementApi.status).mockResolvedValue(status(fields))
    const hook = renderHook(() => useAsrManagement(true)); await settle()
    await act(async () => hook.result.current.start('test-token'))
    expect(managementApi.command).not.toHaveBeenCalled()
    expect(hook.result.current.commandError?.code).toBe('MANAGEMENT_STATE_UNAVAILABLE')
  })

  it('requires a token without persisting it', async () => {
    const write = vi.spyOn(Storage.prototype, 'setItem')
    const hook = renderHook(() => useAsrManagement(true)); await settle()
    await act(async () => hook.result.current.start('  '))
    expect(hook.result.current.commandError?.code).toBe('MANAGEMENT_TOKEN_REQUIRED')
    expect(managementApi.command).not.toHaveBeenCalled()
    await act(async () => hook.result.current.start('test-token'))
    expect(write).not.toHaveBeenCalled()
  })

  it('ignores late status from an older refresh, even if the mock transport ignores abort', async () => {
    const old = deferred<ManagementStatus>()
    vi.mocked(managementApi.status).mockReturnValueOnce(old.promise).mockResolvedValue(status({ state: 'stop_unconfirmed' }))
    const hook = renderHook(() => useAsrManagement(true)); await settle()
    act(() => hook.result.current.refresh()); await settle()
    expect(hook.result.current.status?.state).toBe('stop_unconfirmed')
    await act(async () => old.resolve(ready()))
    expect(hook.result.current.status?.state).toBe('stop_unconfirmed')
  })

  it('clears old status on node selection and ignores the previous node response', async () => {
    const otherNode = { ...node, nodeId: 'd'.repeat(32) }, otherInstance = { ...instance, nodeId: otherNode.nodeId, instanceId: 'e'.repeat(32) }
    const old = deferred<ManagementStatus>()
    vi.mocked(managementApi.nodes).mockResolvedValue([node, otherNode])
    vi.mocked(managementApi.instances).mockImplementation(async id => id === NODE ? [instance] : [otherInstance])
    vi.mocked(managementApi.status).mockReturnValueOnce(old.promise).mockResolvedValue(status({ nodeId: otherNode.nodeId, instanceId: otherInstance.instanceId }))
    const hook = renderHook(() => useAsrManagement(true)); await settle()
    act(() => hook.result.current.selectNode(otherNode.nodeId)); await settle()
    expect(hook.result.current.selectedInstanceId).toBe(otherInstance.instanceId)
    await act(async () => old.resolve(ready()))
    expect(hook.result.current.status?.instanceId).toBe(otherInstance.instanceId)
  })

  it('rejects management status from a different node', async () => {
    vi.mocked(managementApi.status).mockResolvedValue(status({ nodeId: 'd'.repeat(32) }))
    const hook = renderHook(() => useAsrManagement(true)); await settle()
    expect(hook.result.current.status).toBeNull()
    expect(hook.result.current.error?.code).toBe('INVALID_RESPONSE')
  })

  it('expires a previously ready observation during slow directory reads, blocking stale commands', async () => {
    vi.mocked(managementApi.status).mockResolvedValue(ready())
    const hook = renderHook(() => useAsrManagement(true)); await settle()
    const slowNodes = deferred<RegisteredNode[]>()
    vi.mocked(managementApi.nodes).mockReturnValue(slowNodes.promise)
    await act(async () => { await vi.advanceTimersByTimeAsync(10_001) })
    expect(hook.result.current.status).toBeNull()
    expect(hook.result.current.error?.code).toBe('MANAGEMENT_OBSERVATION_EXPIRED')
    await act(async () => hook.result.current.stop('test-token'))
    expect(managementApi.command).not.toHaveBeenCalled()
    await act(async () => slowNodes.resolve([node]))
    expect(hook.result.current.status?.state).toBe('ready')
    expect(hook.result.current.error).toBeNull()
  })

  it.each(['node', 'instance'])('invalidates ready immediately when the selected %s disappears from the registry', async kind => {
    vi.mocked(managementApi.status).mockResolvedValue(ready())
    const hook = renderHook(() => useAsrManagement(true)); await settle()
    const replacementNode = { ...node, nodeId: 'd'.repeat(32) }
    const replacementInstance = { ...instance, nodeId: kind === 'node' ? replacementNode.nodeId : NODE, instanceId: 'e'.repeat(32) }
    const delayedStatus = deferred<ManagementStatus>(), delayedInstances = deferred<AsrInstance[]>()
    if (kind === 'node') {
      vi.mocked(managementApi.nodes).mockResolvedValue([replacementNode])
      vi.mocked(managementApi.instances).mockReturnValue(delayedInstances.promise)
    } else vi.mocked(managementApi.instances).mockResolvedValue([replacementInstance])
    vi.mocked(managementApi.status).mockReturnValue(delayedStatus.promise)
    await act(async () => { await vi.advanceTimersByTimeAsync(2_000) })
    expect(hook.result.current.status).toBeNull()
    if (kind === 'node') await act(async () => delayedInstances.resolve([replacementInstance]))
    await act(async () => delayedStatus.resolve(status({ nodeId: replacementInstance.nodeId, instanceId: replacementInstance.instanceId })))
    expect(hook.result.current.status?.instanceId).toBe(replacementInstance.instanceId)
  })

  it('does not restore a pre-command poll response after a stop intent is submitted', async () => {
    vi.mocked(managementApi.status).mockResolvedValueOnce(ready())
    const hook = renderHook(() => useAsrManagement(true)); await settle()
    const oldPoll = deferred<ManagementStatus>(), post = deferred<ManagementStatus>()
    vi.mocked(managementApi.status).mockReturnValue(oldPoll.promise)
    await act(async () => { await vi.advanceTimersByTimeAsync(2_000) })
    vi.mocked(managementApi.command).mockReturnValue(post.promise)
    act(() => { void hook.result.current.stop('test-token') })
    await act(async () => oldPoll.resolve(ready()))
    expect(hook.result.current.status).toBeNull()
    vi.mocked(managementApi.status).mockResolvedValue(status({ state: 'stopping' }))
    await act(async () => post.resolve(status({ state: 'stopping' })))
    expect(hook.result.current.status?.state).toBe('stopping')
    expect(managementApi.command).toHaveBeenCalledTimes(1)
  })

  it.each(['workspace', 'visibility', 'pagehide'])('invalidates observations and stops polling on %s', async reason => {
    vi.mocked(managementApi.status).mockResolvedValue(ready())
    const hook = renderHook(({ active }) => useAsrManagement(active), { initialProps: { active: true } }); await settle()
    expect(hook.result.current.status?.state).toBe('ready')
    if (reason === 'workspace') hook.rerender({ active: false })
    else if (reason === 'visibility') act(() => visibility('hidden'))
    else act(() => window.dispatchEvent(new Event('pagehide')))
    expect(hook.result.current.status).toBeNull()
    const count = vi.mocked(managementApi.nodes).mock.calls.length
    await act(async () => { await vi.advanceTimersByTimeAsync(10_000) })
    expect(managementApi.nodes).toHaveBeenCalledTimes(count)
    if (reason === 'workspace') hook.rerender({ active: true })
    else if (reason === 'visibility') act(() => visibility('visible'))
    else act(() => window.dispatchEvent(new Event('pageshow')))
    await settle(); expect(hook.result.current.status?.state).toBe('ready')
    expect(managementApi.command).not.toHaveBeenCalled()
  })

  it('aborts an in-flight command when leaving and ignores its eventual receipt', async () => {
    const post = deferred<ManagementStatus>()
    vi.mocked(managementApi.command).mockReturnValue(post.promise)
    const hook = renderHook(({ active }) => useAsrManagement(active), { initialProps: { active: true } }); await settle()
    act(() => { void hook.result.current.start('test-token') })
    const signal = vi.mocked(managementApi.command).mock.calls[0][4]
    hook.rerender({ active: false }); await settle()
    expect(signal?.aborted).toBe(true)
    await act(async () => post.resolve(ready()))
    expect(hook.result.current.status).toBeNull()
    expect(hook.result.current.commandNotice).toBeNull()
    expect(hook.result.current.commandPending).toBeNull()
    expect(managementApi.command).toHaveBeenCalledTimes(1)
  })
})

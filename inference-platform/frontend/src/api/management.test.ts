/** Contract substitutes only; no Agent or model is started by these tests. */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { managementApi } from './management'

const NODE = 'a'.repeat(32), INSTANCE = 'b'.repeat(32), SESSION = 'c'.repeat(32)
const node = { nodeId: NODE, hostname: 'test-laptop', osName: 'Windows', osVersion: 'test', architecture: 'AMD64', cpuCount: 4, gpuStatus: 'not_probed', registrationState: 'registered', agentStatus: 'online', registeredAt: '2026-10-14T00:00:00Z', metadataObservedAt: '2026-10-14T00:00:00Z' }
const instance = { instanceId: INSTANCE, nodeId: NODE, instanceKey: 'asr-default', modelId: 'whisper-small', modelVersion: 'registered-version', adapterKey: 'faster-whisper', executionDevice: 'cpu', registrationState: 'registered', runtimeState: 'unknown', managementMode: 'node_agent', registeredAt: '2026-10-14T00:00:00Z' }
const status = { instanceId: INSTANCE, nodeId: NODE, managementMode: 'node_agent', state: 'stopped', reportedState: 'stopped', desiredState: 'stopped', agentAvailable: true, agentSessionId: SESSION, agentPid: 10, workerSessionId: null, childPid: null, commandRevision: 4, modelReady: false, acceptingTasks: false, activeTaskId: null, lastError: null, heartbeatAt: '2026-10-14T00:00:00Z', exitConfirmedAt: null, shutdownConfirmedAt: null, evaluatedAt: '2026-10-14T00:00:00Z' }
const fetchMock = vi.fn()
const response = (data: unknown) => new Response(JSON.stringify({ data, requestId: 'management-request' }), { headers: { 'Content-Type': 'application/json' } })
const page = (items: unknown[], total = items.length, number = 1) => ({ items, total, page: number, pageSize: 100 })
beforeEach(() => { fetchMock.mockReset(); vi.stubGlobal('fetch', fetchMock) })
afterEach(() => vi.unstubAllGlobals())

describe('ASR management transport', () => {
  it('reads the real complete paginated node registry and filters instances by selected node', async () => {
    fetchMock.mockResolvedValueOnce(response(page([node], 2))).mockResolvedValueOnce(response(page([{ ...node, nodeId: 'd'.repeat(32) }], 2, 2))).mockResolvedValueOnce(response(page([instance])))
    expect(await managementApi.nodes()).toHaveLength(2)
    expect(await managementApi.instances(NODE)).toEqual([instance])
    expect(fetchMock.mock.calls.map(call => call[0])).toEqual(['/api/nodes?page=1&pageSize=100', '/api/nodes?page=2&pageSize=100', `/api/instances?nodeId=${NODE}&page=1&pageSize=100`])
    expect(fetchMock.mock.calls.every(call => call[1].cache === 'no-store')).toBe(true)
  })

  it('keeps an empty registry empty without creating a display-only instance', async () => {
    fetchMock.mockImplementation(async () => response(page([])))
    expect(await managementApi.nodes()).toEqual([])
    expect(await managementApi.instances(NODE)).toEqual([])
  })

  it.each([
    [{ ...page([node]), total: '1' }],
    [page([node], 2), page([node], 2, 2)],
    [page([], 1)],
    [{ ...page([node]), pageSize: 20 }],
  ])('rejects malformed or repeated pagination instead of inventing a complete registry', async (...responses) => {
    for (const item of responses) fetchMock.mockResolvedValueOnce(response(item))
    await expect(managementApi.nodes()).rejects.toMatchObject({ code: 'INVALID_RESPONSE' })
  })

  it('rejects an instance from another node and management status from another instance', async () => {
    fetchMock.mockResolvedValueOnce(response(page([{ ...instance, nodeId: 'd'.repeat(32) }]))).mockResolvedValueOnce(response({ ...status, instanceId: 'd'.repeat(32) }))
    await expect(managementApi.instances(NODE)).rejects.toMatchObject({ code: 'INVALID_RESPONSE' })
    await expect(managementApi.status(INSTANCE)).rejects.toMatchObject({ code: 'INVALID_RESPONSE' })
  })

  it.each([{ agentAvailable: 'true' }, { commandRevision: -1 }, { state: 'terminated' }, { agentSessionId: 'other' }, { modelReady: undefined }])('rejects invalid management fields %j', async fields => {
    fetchMock.mockResolvedValue(response({ ...status, ...fields }))
    await expect(managementApi.status(INSTANCE)).rejects.toMatchObject({ code: 'INVALID_RESPONSE' })
  })

  it('uses the fixed route and current session/revision, carrying the token only in a header', async () => {
    fetchMock.mockResolvedValue(response({ ...status, state: 'starting', desiredState: 'running' }))
    const storage = vi.spyOn(Storage.prototype, 'setItem')
    const command = { agentSessionId: SESSION, expectedRevision: 4 }
    const result = await managementApi.command(INSTANCE, 'start', command, 'test-only-secret')
    expect(result.state).toBe('starting')
    const [url, init] = fetchMock.mock.calls[0]
    expect(url).toBe(`/api/instances/${INSTANCE}/management/start`)
    expect(init.method).toBe('POST')
    expect(init.headers['X-Management-Token']).toBe('test-only-secret')
    expect(JSON.parse(init.body)).toEqual(command)
    expect(url + init.body).not.toContain('test-only-secret')
    expect(storage).not.toHaveBeenCalled()
    storage.mockRestore()
  })

  it('preserves stale-command errors and does not replay the POST', async () => {
    fetchMock.mockResolvedValue(new Response(JSON.stringify({ error: { code: 'AGENT_COMMAND_STALE', message: '会话已变化' }, requestId: 'stale-request' }), { status: 409 }))
    await expect(managementApi.command(INSTANCE, 'stop', { agentSessionId: SESSION, expectedRevision: 4 }, 'test-token')).rejects.toMatchObject({ status: 409, code: 'AGENT_COMMAND_STALE', requestId: 'stale-request' })
    expect(fetchMock).toHaveBeenCalledOnce()
  })
})

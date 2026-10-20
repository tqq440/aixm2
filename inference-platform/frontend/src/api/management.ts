import { ApiError, request } from './client'
import type { AsrInstance, ManagementAction, ManagementCommand, ManagementStatus, RegisteredNode } from '../types/management'

const ID = /^[0-9a-f]{32}$/
const STATES = ['stopped', 'starting', 'ready', 'busy', 'stopping', 'stop_unconfirmed', 'failed', 'unknown']
const record = (value: unknown): value is Record<string, unknown> => typeof value === 'object' && value !== null && !Array.isArray(value)
const id = (value: unknown): value is string => typeof value === 'string' && ID.test(value)
const text = (value: unknown): value is string => typeof value === 'string'
const optionalText = (value: unknown) => value === null || text(value)
const optionalPid = (value: unknown) => value === null || (Number.isSafeInteger(value) && Number(value) > 0)
const invalid = () => new ApiError('后台管理数据与当前节点或实例不匹配，请刷新后核对。', 'INVALID_RESPONSE', 200)

function node(value: unknown): value is RegisteredNode {
  return record(value) && id(value.nodeId) && ['hostname', 'osName', 'osVersion', 'architecture', 'registeredAt', 'metadataObservedAt'].every(key => text(value[key]))
    && optionalPid(value.cpuCount) && value.gpuStatus === 'not_probed' && value.registrationState === 'registered'
    && ['not_started', 'online', 'offline', 'stale'].includes(String(value.agentStatus))
}

function instance(value: unknown): value is AsrInstance {
  return record(value) && id(value.instanceId) && id(value.nodeId) && value.instanceKey === 'asr-default'
    && ['modelId', 'modelVersion', 'adapterKey', 'registeredAt'].every(key => text(value[key]))
    && value.executionDevice === 'cpu' && value.registrationState === 'registered' && value.runtimeState === 'unknown'
    && ['legacy_worker', 'node_agent'].includes(String(value.managementMode))
}

function status(value: unknown, instanceId: string): ManagementStatus {
  if (!record(value) || value.instanceId !== instanceId || !id(value.nodeId) || value.managementMode !== 'node_agent'
    || !STATES.includes(String(value.state)) || !['running', 'stopped'].includes(String(value.desiredState))
    || !['agentAvailable', 'modelReady', 'acceptingTasks'].every(key => typeof value[key] === 'boolean')
    || !['reportedState', 'activeTaskId', 'lastError', 'heartbeatAt', 'exitConfirmedAt', 'shutdownConfirmedAt'].every(key => optionalText(value[key]))
    || !['agentSessionId', 'workerSessionId'].every(key => value[key] === null || id(value[key]))
    || !['agentPid', 'childPid'].every(key => optionalPid(value[key]))
    || !Number.isSafeInteger(value.commandRevision) || Number(value.commandRevision) < 0 || !text(value.evaluatedAt)) throw invalid()
  return value as unknown as ManagementStatus
}

/** The selectors must include the real complete registry, rather than silently dropping later pages. */
async function registry<T>(path: string, guard: (value: unknown) => value is T, identity: (item: T) => string, signal?: AbortSignal): Promise<T[]> {
  const items: T[] = []
  const seen = new Set<string>()
  for (let page = 1; page <= 1_000; page += 1) {
    const data = await request<unknown>(`${path}${path.includes('?') ? '&' : '?'}page=${page}&pageSize=100`, { signal })
    if (!record(data) || !Array.isArray(data.items) || !Number.isSafeInteger(data.total) || Number(data.total) < 0
      || data.page !== page || data.pageSize !== 100 || data.items.length > 100) throw invalid()
    for (const item of data.items) {
      if (!guard(item) || seen.has(identity(item))) throw invalid()
      seen.add(identity(item)); items.push(item)
    }
    if (items.length >= Number(data.total)) return items
    if (data.items.length === 0) throw invalid()
  }
  throw invalid()
}

export const managementApi = {
  nodes(signal?: AbortSignal): Promise<RegisteredNode[]> {
    return registry('/nodes', node, item => item.nodeId, signal)
  },
  instances(nodeId: string, signal?: AbortSignal): Promise<AsrInstance[]> {
    return registry(`/instances?nodeId=${encodeURIComponent(nodeId)}`, (value): value is AsrInstance => instance(value) && value.nodeId === nodeId, item => item.instanceId, signal)
  },
  async status(instanceId: string, signal?: AbortSignal): Promise<ManagementStatus> {
    return status(await request<unknown>(`/instances/${encodeURIComponent(instanceId)}/management`, { signal }), instanceId)
  },
  async command(instanceId: string, action: ManagementAction, command: ManagementCommand, token: string, signal?: AbortSignal): Promise<ManagementStatus> {
    // The token is carried only by this request. No storage, query strings or automatic mutation retry.
    return status(await request<unknown>(`/instances/${encodeURIComponent(instanceId)}/management/${action}`, {
      method: 'POST', signal, headers: { 'Content-Type': 'application/json', 'X-Management-Token': token },
      body: JSON.stringify(command),
    }), instanceId)
  },
}

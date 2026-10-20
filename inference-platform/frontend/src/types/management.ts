/** Registration describes configuration; only ManagementStatus describes observed runtime state. */
export interface RegisteredNode {
  nodeId: string
  hostname: string
  osName: string
  osVersion: string
  architecture: string
  cpuCount: number | null
  gpuStatus: 'not_probed'
  registrationState: 'registered'
  agentStatus: 'not_started' | 'online' | 'offline' | 'stale'
  registeredAt: string
  metadataObservedAt: string
}

export interface AsrInstance {
  instanceId: string
  nodeId: string
  instanceKey: 'asr-default'
  modelId: string
  modelVersion: string
  adapterKey: string
  executionDevice: 'cpu'
  registrationState: 'registered'
  runtimeState: 'unknown'
  managementMode: 'legacy_worker' | 'node_agent'
  registeredAt: string
}

export type ManagementState = 'stopped' | 'starting' | 'ready' | 'busy' | 'stopping' | 'stop_unconfirmed' | 'failed' | 'unknown'
export type ManagementAction = 'start' | 'stop'

export interface ManagementStatus {
  instanceId: string
  nodeId: string
  managementMode: 'node_agent'
  state: ManagementState
  reportedState: string | null
  desiredState: 'running' | 'stopped'
  agentAvailable: boolean
  agentSessionId: string | null
  agentPid: number | null
  workerSessionId: string | null
  childPid: number | null
  commandRevision: number
  modelReady: boolean
  acceptingTasks: boolean
  activeTaskId: string | null
  lastError: string | null
  heartbeatAt: string | null
  exitConfirmedAt: string | null
  shutdownConfirmedAt: string | null
  evaluatedAt: string
}

export interface ManagementCommand {
  agentSessionId: string
  expectedRevision: number
}

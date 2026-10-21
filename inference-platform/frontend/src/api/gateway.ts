import { request } from '@/api/client'

export interface GatewayKey { keyId: string; label: string; revokedAt: string | null }
export interface GatewayApplication { applicationId: string; version: number; enabled: boolean; concurrencyLimit: number; allowedServices: string[]; keys: GatewayKey[] }
export interface GatewayRequest { id: string; invocationId: string; applicationId: string; serviceId: string; kind: string; status: string; reason: string | null; permitState: string; modelId: string; acceptedAt: string; resultAvailable: boolean }
export interface GatewayEvent { sequence: number; eventType: string; source: string; occurredAt: string; details: Record<string, unknown> }
export interface GatewayEvents { items: GatewayEvent[]; hasMore: boolean; nextSequence: number; historyNotice: string }
export interface GatewayMonitor { assignedDevice: { kind: string; deviceId: string | null } | null; resultPending: boolean; resultSaved: boolean; nodeQueryError: string | null; executionError: string | null; monitoring: { supported?: boolean; remoteState?: string; progress?: number | null; stage?: string | null; queuePosition?: number | null; actualDevice?: { kind: string; deviceId?: string | null; namespace: string } | null; lastTrustedAt?: string | null; lastError?: string | null; pollStopped?: string | null } | null }
export interface GatewayNode { nodeId: string; nodeServiceId: string; serviceId: string; targetId: string; checkedAt: string | null; stale: boolean; snapshot: { acceptingRequests?: boolean; adapterConfigured?: boolean; errorCode?: string | null; issues?: string[] } | null }
export interface GatewayCapacity { resources: { resourceKey: string; concurrencyLimit: number; held: number; uncertain: number }[]; notice: string }
const root = '/admin/gateway'
const json = (body: unknown) => ({ headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
export const gatewayApi = {
  status: () => request<{ enabled: boolean; managementAvailable: boolean; notice: string }>(`${root}/status`),
  applications: () => request<{ items: GatewayApplication[] }>(`${root}/applications`),
  save: (app: Omit<GatewayApplication, 'keys' | 'version'>, expectedVersion: number | null) => request(`${root}/applications/${encodeURIComponent(app.applicationId)}`, { method: 'PUT', ...json({ ...app, expectedVersion }) }),
  issue: (application: string) => request<{ apiKey: string }>(`${root}/applications/${encodeURIComponent(application)}/keys`, { method: 'POST', ...json({ label: 'application' }) }),
  rotate: (application: string, key: string) => request<{ apiKey: string }>(`${root}/applications/${encodeURIComponent(application)}/keys/${key}/rotate`, { method: 'POST', ...json({ label: 'rotated' }) }),
  revoke: (application: string, key: string) => request(`${root}/applications/${encodeURIComponent(application)}/keys/${key}/revoke`, { method: 'POST', ...json({}) }),
  requests: (status = '', page = 1, service = '') => request<{ items: GatewayRequest[]; total: number }>(`${root}/requests?page=${page}&pageSize=20${status ? `&status=${status}` : ''}${service ? `&serviceId=${encodeURIComponent(service)}` : ''}`),
  monitor: (id: string) => request<GatewayMonitor>(`${root}/requests/${id}/monitoring`),
  events: (id: string, after = 0) => request<GatewayEvents>(`${root}/requests/${id}/events?afterSequence=${after}&limit=50`),
  nodes: () => request<{ items: GatewayNode[] }>(`${root}/nodes`),
  refreshNodes: () => request<{ items: GatewayNode[] }>(`${root}/nodes/refresh`, { method: 'POST', ...json({}) }),
  capacity: () => request<GatewayCapacity>('/admin/common-execution/capacity'),
}

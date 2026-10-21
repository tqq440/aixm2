import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { gatewayApi as api } from '@/api/gateway'
import { GatewayPanel } from '@/components/GatewayPanel'

vi.mock('@/api/gateway', () => ({ gatewayApi: { status: vi.fn(), applications: vi.fn(), save: vi.fn(), issue: vi.fn(), rotate: vi.fn(), revoke: vi.fn(), requests: vi.fn(), monitor: vi.fn(), events: vi.fn(), nodes: vi.fn(), refreshNodes: vi.fn(), capacity: vi.fn() } }))
const task = { id: 'a'.repeat(32), invocationId: 'b'.repeat(32), applicationId: 'campus', serviceId: 'asr-main', kind: 'asr', status: 'unknown', reason: 'REMOTE_UNKNOWN', permitState: 'uncertain', modelId: 'asr-model', acceptedAt: '2026-10-21T10:00:00Z', resultAvailable: false }
beforeEach(() => {
  vi.mocked(api.status).mockResolvedValue({ enabled: true, managementAvailable: true, notice: '应用通过Node调用' })
  vi.mocked(api.applications).mockResolvedValue({ items: [{ applicationId: 'campus', version: 1, enabled: true, concurrencyLimit: 1, allowedServices: ['asr-main'], keys: [{ keyId: 'c'.repeat(32), label: 'default', revokedAt: null }] }] })
  vi.mocked(api.requests).mockResolvedValue({ items: [task], total: 1 })
  vi.mocked(api.nodes).mockResolvedValue({ items: [] })
  vi.mocked(api.capacity).mockResolvedValue({ resources: [{ resourceKey: 'group:gpu-shared', concurrencyLimit: 1, held: 0, uncertain: 1 }], notice: '并发许可账本' })
  vi.mocked(api.monitor).mockResolvedValue({ assignedDevice: { kind: 'gpu', deviceId: '0' }, resultPending: true, resultSaved: false, nodeQueryError: null, executionError: null, monitoring: { supported: true, progress: null, actualDevice: { kind: 'cpu', deviceId: null, namespace: 'service' }, lastTrustedAt: null, lastError: 'UPSTREAM_HTTP_404' } })
  vi.mocked(api.events).mockResolvedValue({ items: [{ sequence: 1, eventType: 'invocation.accepted', source: 'platform', occurredAt: '2026-10-21T10:00:00Z', details: {} }], hasMore: true, nextSequence: 1, historyNotice: '详细日志从升级后开始记录' })
})
afterEach(() => { cleanup(); vi.resetAllMocks(); localStorage.clear() })

it('opening only reads saved platform records', async () => {
  render(<GatewayPanel />); await screen.findByRole('button', { name: '创建 Key' })
  expect(api.refreshNodes).not.toHaveBeenCalled(); expect(api.issue).not.toHaveBeenCalled(); expect(api.monitor).not.toHaveBeenCalled()
})

it('does not query unmigrated daily data when management is disabled', async () => {
  vi.mocked(api.status).mockResolvedValue({ enabled: false, managementAvailable: false, notice: '公共调用尚未启用' })
  render(<GatewayPanel />); await screen.findByText(/公共调用尚未启用/)
  expect(api.applications).not.toHaveBeenCalled(); expect(api.requests).not.toHaveBeenCalled()
})

it('shows assigned and reported device separately, without inventing progress', async () => {
  render(<GatewayPanel />); fireEvent.click(await screen.findByRole('button', { name: `查看任务 ${task.id}` }))
  await screen.findByText('GPU 0'); expect(screen.getByText(/CPU（服务内编号）/)).toBeTruthy()
  expect(screen.getByText('服务暂未报告进度')).toBeTruthy(); expect(screen.getByText('服务已完成，结果待获取')).toBeTruthy()
  expect(screen.getByText('UPSTREAM_HTTP_404')).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: '加载更多事件' }))
  await waitFor(() => expect(api.events).toHaveBeenLastCalledWith(task.invocationId, 1))
  expect(api.refreshNodes).not.toHaveBeenCalled()
})

it('full key is ephemeral, cleared on hide and never stored in localStorage', async () => {
  vi.mocked(api.issue).mockResolvedValue({ apiKey: 'PRIVATE_API_KEY_ONCE' })
  render(<GatewayPanel />); fireEvent.click(await screen.findByRole('button', { name: '创建 Key' }))
  await screen.findByText('PRIVATE_API_KEY_ONCE'); expect(localStorage.length).toBe(0)
  fireEvent.click(screen.getByRole('button', { name: '隐藏并清除' })); expect(screen.queryByText('PRIVATE_API_KEY_ONCE')).toBeNull()
})

it('filters unknown tasks and refreshes Node only on explicit action', async () => {
  vi.mocked(api.refreshNodes).mockResolvedValue({ items: [] })
  render(<GatewayPanel />); await screen.findByRole('button', { name: '创建 Key' })
  fireEvent.change(screen.getByLabelText('任务状态'), { target: { value: 'unknown' } })
  await waitFor(() => expect(api.requests).toHaveBeenLastCalledWith('unknown', 1, ''))
  fireEvent.click(screen.getByRole('button', { name: '核对节点状态' }))
  await waitFor(() => expect(api.refreshNodes).toHaveBeenCalledTimes(1))
})

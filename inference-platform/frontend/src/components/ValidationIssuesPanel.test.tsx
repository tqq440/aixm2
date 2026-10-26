import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { validationIssuesApi as api } from '@/api/validation-issues'
import type { EnvironmentIssuePage } from '@/api/validation-issues'
import { ValidationIssuesPanel } from '@/components/ValidationIssuesPanel'

vi.mock('@/api/validation-issues', () => ({ validationIssuesApi: { list: vi.fn() } }))
const issue = { stage: 'module6b' as const, target: 'swufefic-v100', code: 'CAMPUS_NETWORK_UNAVAILABLE' as const, state: 'waiting_external_verification' as const, source: 'operator' as const, firstObservedAt: '2026-10-22T01:00:00Z', lastObservedAt: '2026-10-22T02:00:00Z', observationCount: 1, evidenceSha256: 'a'.repeat(64) }
const page = (changes: Partial<EnvironmentIssuePage> = {}): EnvironmentIssuePage => ({ items: [issue], total: 1, offset: 0, limit: 50, unreadableCount: 0, scanTruncated: false, notice: '本地记录', ...changes })
beforeEach(() => vi.mocked(api.list).mockResolvedValue(page()))
afterEach(cleanup)

it('shows deferral as waiting and never as task failure or validation success', async () => {
  render(<ValidationIssuesPanel />)
  await screen.findByText('swufefic-v100')
  expect(screen.getByText('待外部验收')).toBeTruthy()
  expect(screen.getByText(/校园网暂不可达/)).toBeTruthy()
  expect(screen.getByText(/人工确认/)).toBeTruthy()
  expect(screen.queryByText('已通过')).toBeNull(); expect(screen.queryByText('任务失败')).toBeNull()
  expect(api.list).toHaveBeenCalledTimes(1)
})

it('empty, unreadable and partial lists never imply validation passed', async () => {
  vi.mocked(api.list).mockResolvedValue(page({ items: [], total: 0, unreadableCount: 2, scanTruncated: true }))
  render(<ValidationIssuesPanel />)
  await screen.findByText(/有 2 份记录无法读取/)
  expect(screen.getByText(/清单并不完整/)).toBeTruthy()
  expect(screen.getByText(/实际验收状态仍需核实/)).toBeTruthy()
})

it('refresh is manual, only re-reads snapshots and retains old data on failure', async () => {
  render(<ValidationIssuesPanel />); await screen.findByText('swufefic-v100')
  await waitFor(() => expect((screen.getByRole('button', { name: '刷新验收记录' }) as HTMLButtonElement).disabled).toBe(false))
  vi.mocked(api.list).mockRejectedValueOnce(new Error('PRIVATE_RAW_RESPONSE'))
  fireEvent.click(screen.getByRole('button', { name: '刷新验收记录' }))
  await screen.findByRole('alert')
  expect(screen.getByText('swufefic-v100')).toBeTruthy(); expect(screen.queryByText(/PRIVATE_RAW_RESPONSE/)).toBeNull()
  expect(api.list).toHaveBeenCalledTimes(2)
})

it('ignores stale filter replies and aborts the superseded read', async () => {
  let release!: (value: EnvironmentIssuePage) => void
  vi.mocked(api.list).mockReturnValueOnce(new Promise(resolve => { release = resolve }))
    .mockResolvedValueOnce(page({ items: [{ ...issue, target: 'current-node' }] }))
  render(<ValidationIssuesPanel />)
  const firstSignal = vi.mocked(api.list).mock.calls[0][2]!
  fireEvent.change(screen.getByLabelText('验收阶段'), { target: { value: 'module6b' } })
  await screen.findByText('current-node')
  await act(async () => release(page()))
  expect(firstSignal.aborted).toBe(true); expect(screen.queryByText('swufefic-v100')).toBeNull()
  expect(api.list).toHaveBeenLastCalledWith('module6b', 0, expect.anything())
})

it('reads next page without losing the way back when records disappear', async () => {
  vi.mocked(api.list).mockResolvedValueOnce(page({ total: 51 })).mockResolvedValueOnce(page({ items: [], total: 0, offset: 50 }))
  render(<ValidationIssuesPanel />); await screen.findByText('swufefic-v100')
  fireEvent.click(screen.getByRole('button', { name: '下一页验收记录' }))
  await screen.findByText(/当前筛选下没有可展示/)
  expect((screen.getByRole('button', { name: '上一页验收记录' }) as HTMLButtonElement).disabled).toBe(false)
  expect(api.list).toHaveBeenLastCalledWith('', 50, expect.anything())
})

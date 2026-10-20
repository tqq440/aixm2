import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { ApiError } from '@/api/client'
import { AsrSchedulingPanel } from '@/components/AsrSchedulingPanel'
import { catalogFixture, resourceFixture, schedulingFixture, targetFixture } from '@/test/asrSchedulingFixtures'
import type { useAsrScheduling } from '@/hooks/useAsrScheduling'

function state(changes: Partial<ReturnType<typeof useAsrScheduling>> = {}): ReturnType<typeof useAsrScheduling> {
  return { status: schedulingFixture(), targets: catalogFixture(), statusError: null, targetsError: null,
    loading: false, offset: 0, pageSize: 5, now: Date.now(), refresh: vi.fn(), setOffset: vi.fn(), statusTrusted: true, targetsTrusted: true, ...changes }
}
afterEach(cleanup)

it('keeps fixed mode, declared hardware and reported capacity distinct', () => {
  render(<AsrSchedulingPanel scheduling={state()} />)
  expect(screen.getByText('原固定通道')).toBeTruthy()
  expect(screen.getByText(/本机 Whisper 不是外部模型的 CPU 替代/)).toBeTruthy()
  fireEvent.click(screen.getByText('目标与资源报告'))
  expect(screen.getByText('GPU 声明 · 待核实')).toBeTruthy()
  expect(screen.getByText(/平台配置限额：目标 1 \/ 共享组 1/)).toBeTruthy()
  expect(screen.getByText('报告可用并发')).toBeTruthy()
  expect(screen.getByText(/隔离测试资料.*报告就绪/)).toBeTruthy()
  expect(screen.queryByText('可以转写')).toBeNull()
  expect(screen.queryByRole('button', { name: /启用|启动|停止|释放/ })).toBeNull()
})

it.each(['stale', 'unknown'] as const)('never presents expired or unknown samples as available (%s)', freshness => {
  render(<AsrSchedulingPanel scheduling={state({ targets: catalogFixture({ items: [targetFixture({ observations: [resourceFixture({ freshness })] })] }) })} />)
  expect(screen.getByText(freshness === 'stale' ? '采样已过期' : '采样未确认')).toBeTruthy()
  expect(screen.queryByText('2048 MiB')).toBeNull()
  expect(screen.queryByText('报告就绪')).toBeNull()
})

it('expires at the exact boundary and treats clock rollback before recording as unknown', () => {
  const now = Date.now()
  const report = resourceFixture({ observedAt: new Date(now - 60_000).toISOString(), recordedAt: new Date(now - 10_000).toISOString(), expiresAt: new Date(now).toISOString() })
  const view = render(<AsrSchedulingPanel scheduling={state({ now, targets: catalogFixture({ items: [targetFixture({ observations: [report] })] }) })} />)
  expect(screen.getByText('采样已过期')).toBeTruthy()
  view.rerender(<AsrSchedulingPanel scheduling={state({ now: now - 20_000, targets: catalogFixture({ items: [targetFixture({ observations: [report] })] }) })} />)
  expect(screen.getByText('采样未确认')).toBeTruthy()
})

it('shows directory failure and suppresses previous sampled values', () => {
  render(<AsrSchedulingPanel scheduling={state({ targetsError: new ApiError('隔离断网'), targetsTrusted: false })} />)
  expect(screen.getByText('目标目录读取失败')).toBeTruthy()
  expect(screen.getByText(/上次报告不能作为当前可用资源/)).toBeTruthy()
  expect(screen.queryByText('2048 MiB')).toBeNull()
  expect(screen.getByText('采样未确认')).toBeTruthy()
})

it('shows isolated scope without enabling it and calls only refresh and pagination handlers', () => {
  const value = state({ status: schedulingFixture({ mode: 'isolated', routingConfigured: true, executionScope: 'isolated', candidateCount: 2 }), targets: catalogFixture({ total: 7 }) })
  render(<AsrSchedulingPanel scheduling={value} />)
  expect(screen.getByText('隔离调度验证')).toBeTruthy()
  expect(screen.getByText(/不代表实验室 GPU、CPU 或真实容量已接通/)).toBeTruthy()
  fireEvent.click(screen.getByText('目标与资源报告'))
  fireEvent.click(screen.getByRole('button', { name: '下一页目标' }))
  expect(value.setOffset).toHaveBeenCalledWith(5)
  fireEvent.click(screen.getByRole('button', { name: '刷新调度信息' }))
  expect(value.refresh).toHaveBeenCalledOnce()
})

it('explains unknown outcome without offering retry or release controls', () => {
  render(<AsrSchedulingPanel scheduling={state()} />)
  const details = screen.getByText('为什么任务还在等待？').closest('details')!
  expect(details.textContent).toContain('不会复制到 CPU 或其他目标重跑')
  expect(details.textContent).toContain('是否仍占用许可，以调用记录和当前调用容量为准')
  expect(details.textContent).not.toContain('继续占用许可')
  expect(within(details).queryByRole('button')).toBeNull()
})

it('renders an empty directory and does not invent a GPU or concurrency reading', () => {
  render(<AsrSchedulingPanel scheduling={state({ targets: catalogFixture({ total: 0, items: [] }) })} />)
  expect(screen.getByText(/尚未登记计算目标/)).toBeTruthy()
  expect(screen.queryByText('GPU 声明 · 待核实')).toBeNull()
  expect(screen.queryByText('报告可用并发')).toBeNull()
})

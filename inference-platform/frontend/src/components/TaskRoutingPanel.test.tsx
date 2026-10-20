import { act, cleanup, render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { TaskRoutingPanel } from '@/components/TaskRoutingPanel'
import { taskRoutingFixture } from '@/test/asrSchedulingFixtures'
import type { TaskView } from '@/types/api'

const task = (changes: Partial<TaskView> = {}) => ({ status: 'queued', remoteOutcome: 'not_dispatched', routing: taskRoutingFixture(), ...changes }) as TaskView
afterEach(() => { cleanup(); vi.useRealTimers() })

it('shows detailed waiting reason without claiming execution authority', () => {
  render(<TaskRoutingPanel task={task()} untrusted={false} />)
  expect(screen.getByText('等待选择执行目标')).toBeTruthy()
  expect(screen.getByText(/GPU资源报告尚未确认/)).toBeTruthy()
  expect(screen.getByText('资源报告已过期，请核对新的观测。')).toBeTruthy()
  expect(screen.queryByRole('button')).toBeNull()
})

it('expires cached waiting diagnosis while the task refresh is hung', async () => {
  vi.useFakeTimers()
  render(<TaskRoutingPanel task={task()} untrusted={false} />)
  await act(async () => vi.advanceTimersByTimeAsync(10_001))
  expect(screen.getByText('调度原因未确认')).toBeTruthy()
  expect(screen.queryByText('资源报告已过期，请核对新的观测。')).toBeNull()
})

it('keeps selected target history but does not repeat recommendations after unknown outcome', () => {
  const routing = taskRoutingFixture({ selectionState: 'selected', catalogRevisionId: '3'.repeat(64), targetId: 'fixture-gpu',
    deviceKind: 'gpu', selectedAt: new Date().toISOString(), reasonCode: 'GPU_PREFERRED', reasonCategory: 'outcome_unknown',
    statusCode: 'REMOTE_ASR_RESULT_UNKNOWN', message: '不能重发' })
  render(<TaskRoutingPanel task={task({ routing, status: 'failed', remoteOutcome: 'unknown' })} untrusted={false} />)
  expect(screen.getByText('原目标结果待核对')).toBeTruthy()
  expect(screen.getByText(/不会自动转到 CPU 或其他目标重发/)).toBeTruthy()
  expect(screen.getByText(/是否仍占用许可，请核对调用记录与当前调用容量/)).toBeTruthy()
  expect(screen.queryByText(/保留原目标与占位|继续占用许可/)).toBeNull()
  expect(screen.queryByText('查看候选等待原因')).toBeNull()
  expect(screen.queryByText('GPU_PREFERRED')).toBeNull()
  expect(screen.queryByText(routing.routingRevisionId)).toBeNull()
})

it('does not add routing history to local or old external tasks', () => {
  const view = render(<TaskRoutingPanel task={task({ routing: undefined, remoteOutcome: undefined })} untrusted={false} />)
  expect(view.container.textContent).toBe('')
  view.rerender(<TaskRoutingPanel task={task({ routing: undefined })} untrusted={false} />)
  expect(screen.getByText(/原固定通道任务：未记录 GPU \/ CPU 选择/)).toBeTruthy()
})

it('discards a waiting recommendation after a task detail error', () => {
  render(<TaskRoutingPanel task={task()} untrusted />)
  expect(screen.getByText('调度原因未确认')).toBeTruthy()
  expect(screen.queryByText('资源报告已过期，请核对新的观测。')).toBeNull()
})

it('distinguishes an ended unselected task from waiting for resources', () => {
  render(<TaskRoutingPanel task={task({ status: 'cancelled', routing: taskRoutingFixture({ reasonCategory: 'terminal', message: '排队任务已取消，不再选择目标。' }) })} untrusted={false} />)
  expect(screen.getByText('任务已结束，未选择目标')).toBeTruthy()
  expect(screen.getByText('排队任务已取消，不再选择目标。')).toBeTruthy()
  expect(screen.queryByText('查看候选等待原因')).toBeNull()
})

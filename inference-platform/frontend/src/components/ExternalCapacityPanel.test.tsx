import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { ExternalCapacityPanel } from './ExternalCapacityPanel'
import { capacityFixture } from '../test/asrCapacityFixtures'

afterEach(cleanup)

it('shows shared occupancy including unknown results, separate slots and task selection', () => {
  const onSelectTask = vi.fn()
  render(<ExternalCapacityPanel value={capacityFixture()} trusted onSelectTask={onSelectTask} />)
  expect(screen.getAllByText('2 / 2')).toHaveLength(2)
  expect(screen.getByText('设备：未知')).toBeTruthy()
  expect(screen.getByText(/不会自动重发到其他槽或设备/)).toBeTruthy()
  expect(screen.getByText('REMOTE_ASR_RESULT_UNKNOWN')).toBeTruthy()
  expect(screen.getByText('空闲，等待配额')).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: '查看调用槽 1 的任务' }))
  expect(onSelectTask).toHaveBeenCalledWith('3'.repeat(32))
  expect(screen.queryByRole('button', { name: /启动|停止|重试/ })).toBeNull()
})

it.each([false, true])('suppresses current counts and task links when untrusted or stale (%s)', trusted => {
  const value = capacityFixture()
  if (trusted) value.observedAt = new Date(Date.now() - 20_000).toISOString()
  render(<ExternalCapacityPanel value={value} trusted={trusted} onSelectTask={vi.fn()} />)
  expect(screen.getByText(/容量状态未确认/)).toBeTruthy()
  expect(screen.queryByText('2 / 2')).toBeNull()
  expect(screen.queryByRole('button')).toBeNull()
})

it('distinguishes old configuration queue and stale slot from readiness', () => {
  const value = capacityFixture()
  value.incompatibleQueued = 2
  value.slots[0] = { ...value.slots[0], state: 'stale', fresh: false, modelReady: false }
  render(<ExternalCapacityPanel value={value} trusted onSelectTask={vi.fn()} />)
  expect(screen.getByText('心跳已过期')).toBeTruthy()
  expect(screen.getByText(/2 个任务等待原配置/)).toBeTruthy()
})

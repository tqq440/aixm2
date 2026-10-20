import { act, cleanup, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { useAsrScheduling } from '@/hooks/useAsrScheduling'
import { readAsrScheduling, readAsrTargets } from '@/api/asr-scheduling'
import { catalogFixture, schedulingFixture } from '@/test/asrSchedulingFixtures'
import type { AsrCatalogPage } from '@/types/asrScheduling'

vi.mock('@/api/asr-scheduling', () => ({ readAsrScheduling: vi.fn(), readAsrTargets: vi.fn() }))
const tick = async (milliseconds = 0) => { await act(async () => { await vi.advanceTimersByTimeAsync(milliseconds) }) }
beforeEach(() => {
  vi.useFakeTimers()
  Object.defineProperty(document, 'visibilityState', { configurable: true, value: 'visible' })
  vi.mocked(readAsrScheduling).mockImplementation(async () => schedulingFixture())
  vi.mocked(readAsrTargets).mockImplementation(async offset => catalogFixture({ offset, total: 11 }))
})
afterEach(() => { cleanup(); vi.useRealTimers(); vi.resetAllMocks() })

it('loads independent mode and directory snapshots and does not mistake one failure for success', async () => {
  vi.mocked(readAsrTargets).mockRejectedValueOnce(new Error('directory offline'))
  const { result } = renderHook(() => useAsrScheduling())
  await tick()
  expect(result.current.statusTrusted).toBe(true)
  expect(result.current.targetsTrusted).toBe(false)
  expect(result.current.targetsError).not.toBeNull()
})

it('expires previous responses while the next poll hangs without overlapping requests', async () => {
  vi.mocked(readAsrTargets).mockResolvedValueOnce(catalogFixture()).mockImplementation(() => new Promise(() => {}))
  const { result } = renderHook(() => useAsrScheduling())
  await tick()
  expect(result.current.statusTrusted).toBe(true)
  expect(result.current.targetsTrusted).toBe(true)
  await tick(10_001)
  expect(result.current.statusTrusted).toBe(false)
  expect(result.current.targetsTrusted).toBe(false)
  expect(readAsrTargets).toHaveBeenCalledTimes(2)
})

it('discards old page results and never labels the preceding page as current', async () => {
  let finish!: (value: AsrCatalogPage) => void
  vi.mocked(readAsrTargets).mockImplementationOnce(() => new Promise(resolve => { finish = resolve }))
  const { result } = renderHook(() => useAsrScheduling())
  const firstSignal = vi.mocked(readAsrTargets).mock.calls[0][2]!
  act(() => result.current.setOffset(5)); await tick()
  expect(firstSignal.aborted).toBe(true)
  expect(result.current.targets?.offset).toBe(5)
  await act(async () => finish(catalogFixture({ offset: 0 })))
  expect(result.current.targets?.offset).toBe(5)
})

it('aborts on workspace switch and refreshes on resuming ASR', async () => {
  const { result, rerender } = renderHook(({ active }) => useAsrScheduling(active), { initialProps: { active: true } })
  await tick()
  const signal = vi.mocked(readAsrTargets).mock.calls[0][2]!
  rerender({ active: false })
  expect(signal.aborted).toBe(true)
  expect(result.current.targetsTrusted).toBe(false)
  await tick(5_000)
  expect(readAsrTargets).toHaveBeenCalledTimes(1)
  rerender({ active: true }); await tick()
  expect(result.current.targetsTrusted).toBe(true)
  expect(readAsrTargets).toHaveBeenCalledTimes(2)
})

it('stops polling when hidden and immediately invalidates displayed freshness', async () => {
  const { result } = renderHook(() => useAsrScheduling())
  await tick()
  Object.defineProperty(document, 'visibilityState', { configurable: true, value: 'hidden' })
  act(() => document.dispatchEvent(new Event('visibilitychange')))
  expect(result.current.statusTrusted).toBe(false)
  await tick(10_000)
  expect(readAsrTargets).toHaveBeenCalledTimes(1)
  Object.defineProperty(document, 'visibilityState', { configurable: true, value: 'visible' })
  act(() => document.dispatchEvent(new Event('visibilitychange')))
  await tick()
  expect(result.current.statusTrusted).toBe(true)
})

it('refresh failure makes a previous directory explicitly untrusted', async () => {
  const { result } = renderHook(() => useAsrScheduling())
  await tick()
  vi.mocked(readAsrTargets).mockRejectedValueOnce(new Error('offline'))
  act(() => result.current.refresh()); await tick()
  expect(result.current.targets).not.toBeNull()
  expect(result.current.targetsTrusted).toBe(false)
  expect(result.current.targetsError).not.toBeNull()
})

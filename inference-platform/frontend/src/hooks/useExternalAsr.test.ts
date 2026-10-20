import { act, cleanup, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { useExternalAsr } from './useExternalAsr'
import { readExternalAsrStatus } from '../api/external-asr'
import type { ExternalAsrStatus } from '../types/api'

vi.mock('../api/external-asr', () => ({ readExternalAsrStatus: vi.fn() }))
const ready = (): ExternalAsrStatus => ({ configured: true, available: true, state: 'ready', observedAt: new Date().toISOString(), message: 'fixture ready', currentTaskId: null, uncertainTaskId: null, modelId: 'fixture', modelVersion: 'v1' })
const tick = async (ms = 0) => { await act(async () => { await vi.advanceTimersByTimeAsync(ms) }) }

beforeEach(() => {
  vi.useFakeTimers()
  Object.defineProperty(document, 'visibilityState', { configurable: true, value: 'visible' })
  vi.mocked(readExternalAsrStatus).mockImplementation(async () => ready())
})
afterEach(() => { cleanup(); vi.useRealTimers(); vi.resetAllMocks() })

describe('external ASR observation', () => {
  it('fails closed when an old ready response is returned', async () => {
    vi.mocked(readExternalAsrStatus).mockResolvedValue({ ...ready(), observedAt: new Date(Date.now() - 11_000).toISOString() })
    const { result } = renderHook(() => useExternalAsr())
    await tick()
    expect(result.current.status?.state).toBe('ready')
    expect(result.current.stale).toBe(true)
  })

  it('expires a ready snapshot during a hung poll without overlapping requests', async () => {
    vi.mocked(readExternalAsrStatus).mockResolvedValueOnce(ready()).mockImplementation(() => new Promise(() => {}))
    const { result } = renderHook(() => useExternalAsr())
    await tick()
    expect(result.current.stale).toBe(false)
    await tick(10_001)
    expect(result.current.stale).toBe(true)
    expect(readExternalAsrStatus).toHaveBeenCalledTimes(2)
  })

  it('aborts observation when inactive, ignores old responses and refreshes on resume', async () => {
    let resolve!: (value: ExternalAsrStatus) => void
    vi.mocked(readExternalAsrStatus).mockImplementationOnce(() => new Promise(ok => { resolve = ok }))
    const { result, rerender } = renderHook(({ active }) => useExternalAsr(active), { initialProps: { active: true } })
    const signal = vi.mocked(readExternalAsrStatus).mock.calls[0][0]!
    rerender({ active: false })
    expect(signal.aborted).toBe(true)
    await act(async () => resolve(ready()))
    expect(result.current.status).toBeNull()
    rerender({ active: true }); await tick()
    expect(result.current.stale).toBe(false)
  })

  it('replaces a ready observation with uncertainty on a network failure', async () => {
    vi.mocked(readExternalAsrStatus).mockResolvedValueOnce(ready()).mockRejectedValue(new Error('offline'))
    const { result } = renderHook(() => useExternalAsr())
    await tick(); await tick(2_000)
    expect(result.current.stale).toBe(true)
    expect(result.current.error).not.toBeNull()
  })

  it('pauses when the browser hides and checks again when visible', async () => {
    const { result } = renderHook(() => useExternalAsr())
    await tick()
    Object.defineProperty(document, 'visibilityState', { configurable: true, value: 'hidden' })
    act(() => document.dispatchEvent(new Event('visibilitychange')))
    await tick(5_000)
    expect(readExternalAsrStatus).toHaveBeenCalledTimes(1)
    expect(result.current.stale).toBe(true)
    Object.defineProperty(document, 'visibilityState', { configurable: true, value: 'visible' })
    act(() => document.dispatchEvent(new Event('visibilitychange')))
    await tick()
    expect(readExternalAsrStatus).toHaveBeenCalledTimes(2)
    expect(result.current.stale).toBe(false)
  })
})

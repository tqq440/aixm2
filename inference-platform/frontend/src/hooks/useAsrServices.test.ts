import { act, cleanup, renderHook, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { useAsrServices } from './useAsrServices'
import { asrServicesApi } from '@/api/asr-services'
import { ApiError } from '@/api/client'
import { serviceFixture, servicesFixture } from '@/test/asrServicesFixtures'
import type { AsrServicesState } from '@/types/asrServices'

vi.mock('@/api/asr-services', () => ({ asrServicesApi: { list: vi.fn(), save: vi.fn(), select: vi.fn(), remove: vi.fn() } }))
beforeEach(() => vi.mocked(asrServicesApi.list).mockResolvedValue(servicesFixture()))
afterEach(() => { cleanup(); vi.resetAllMocks() })

describe('ASR configuration state', () => {
  it('preserves running settings when selection requires a restart', async () => {
    const selected = servicesFixture({ selectedRevision: 'b'.repeat(64), restartRequired: true })
    vi.mocked(asrServicesApi.select).mockResolvedValue(selected)
    const { result } = renderHook(() => useAsrServices())
    await waitFor(() => expect(result.current.loading).toBe(false))
    await act(async () => { await result.current.select('speech-main', 'b'.repeat(64)) })
    expect(result.current.state?.runningRevision).toBe('a'.repeat(64))
    expect(result.current.state?.restartRequired).toBe(true)
  })
  it('keeps existing state and exposes a concurrent-edit conflict', async () => {
    vi.mocked(asrServicesApi.save).mockRejectedValue(new ApiError('请刷新配置', 'ASR_CONFIG_CHANGED', 409))
    const { result } = renderHook(() => useAsrServices())
    await waitFor(() => expect(result.current.loading).toBe(false))
    await act(async () => { await result.current.save(serviceFixture(), 'a'.repeat(64)) })
    expect(result.current.state).toEqual(servicesFixture())
    expect(result.current.actionError?.code).toBe('ASR_CONFIG_CHANGED')
    expect(asrServicesApi.save).toHaveBeenCalledTimes(1)
  })
  it('ignores an older read arriving after a successful mutation', async () => {
    let resolve!: (value: AsrServicesState) => void
    vi.mocked(asrServicesApi.list).mockImplementationOnce(() => new Promise(ok => { resolve = ok }))
    const saved = servicesFixture({ items: [serviceFixture({ name: '更新配置' })] })
    vi.mocked(asrServicesApi.save).mockResolvedValue(saved)
    const { result } = renderHook(() => useAsrServices())
    await act(async () => { await result.current.save(serviceFixture(), 'a'.repeat(64)) })
    await act(async () => resolve(servicesFixture()))
    expect(result.current.state?.items[0].name).toBe('更新配置')
  })
  it('does not read inactive workspaces and aborts a read when leaving', async () => {
    const { rerender } = renderHook(({ active }) => useAsrServices(active), { initialProps: { active: false } })
    expect(asrServicesApi.list).not.toHaveBeenCalled()
    rerender({ active: true })
    const signal = vi.mocked(asrServicesApi.list).mock.calls[0][0]!
    rerender({ active: false })
    expect(signal.aborted).toBe(true)
  })
})

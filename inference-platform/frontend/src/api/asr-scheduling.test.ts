import { afterEach, describe, expect, it, vi } from 'vitest'
import { readAsrScheduling, readAsrTargets } from '@/api/asr-scheduling'
import { api } from '@/api/client'
import { catalogFixture, schedulingFixture, targetFixture, taskRoutingFixture } from '@/test/asrSchedulingFixtures'

const response = (data: unknown) => new Response(JSON.stringify({ data, requestId: 'isolated' }), { headers: { 'Content-Type': 'application/json' } })
afterEach(() => vi.unstubAllGlobals())

describe('read-only scheduling contracts', () => {
  it('uses only relative GET endpoints with bounded pagination and no control credentials', async () => {
    const fetchMock = vi.fn().mockResolvedValueOnce(response(schedulingFixture())).mockResolvedValueOnce(response(catalogFixture()))
    vi.stubGlobal('fetch', fetchMock)
    expect((await readAsrScheduling()).mode).toBe('legacy')
    expect((await readAsrTargets()).items[0].verificationState).toBe('unverified')
    expect(fetchMock.mock.calls.map(call => call[0])).toEqual(['/api/asr/external/scheduling', '/api/asr/external/targets?offset=0&limit=5'])
    for (const [, options] of fetchMock.mock.calls) {
      expect(options.method).toBeUndefined()
      expect(options.headers).toEqual({ Accept: 'application/json' })
      expect(options.cache).toBe('no-store')
    }
  })

  it.each([null, {}, { mode: 'ready' }, { observedAt: 'invalid' }, { candidateCount: 9 }, { candidateCount: 1.5 },
    { mode: 'isolated', routingConfigured: false, executionScope: 'isolated' },
    { mode: 'legacy', executionScope: 'isolated' }, { mode: 'blocked', executionScope: 'legacy' }])('rejects contradictory mode data %j', async changes => {
    const value = changes === null ? null : Object.keys(changes).length ? schedulingFixture(changes as never) : {}
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(response(value)))
    await expect(readAsrScheduling()).rejects.toMatchObject({ code: 'INVALID_RESPONSE' })
  })

  it.each([{ mode: 'isolated', routingConfigured: true, executionScope: 'isolated', candidateCount: 2 },
    { mode: 'blocked', routingConfigured: true, executionScope: 'disabled', candidateCount: 0 }] as const)('accepts explicit scoped modes %j', async changes => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(response(schedulingFixture(changes))))
    expect(await readAsrScheduling()).toMatchObject(changes)
  })

  it.each([[-1, 5], [1.5, 5], [0, 0], [0, 101]])('rejects invalid pagination before fetch %s/%s', async (offset, limit) => {
    const fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)
    await expect(readAsrTargets(offset, limit)).rejects.toMatchObject({ code: 'INVALID_QUERY' })
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it.each([
    () => ({ ...catalogFixture(), offset: 5 }), () => ({ ...catalogFixture(), limit: 20 }),
    () => ({ ...catalogFixture(), routingEnabled: true }),
    () => catalogFixture({ items: [targetFixture({ verificationState: 'verified' as never })] }),
    () => catalogFixture({ items: [targetFixture({ observations: [{ ...targetFixture().observations[0], usableForScheduling: true as never }] })] }),
    () => catalogFixture({ items: [targetFixture({ observations: [{ ...targetFixture().observations[0], reportedAvailableConcurrency: -1 }] })] }),
    () => catalogFixture({ items: [targetFixture({ observations: [{ ...targetFixture().observations[0], gpuUtilizationPercent: 101 }] })] }),
    () => catalogFixture({ items: [targetFixture({ observations: [targetFixture().observations[0], targetFixture().observations[0]] })] }),
    () => catalogFixture({ total: 2, items: [targetFixture(), targetFixture()] }),
  ])('rejects invalid, duplicated or execution-authorizing catalog data', async value => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(response(value())))
    await expect(readAsrTargets()).rejects.toMatchObject({ code: 'INVALID_RESPONSE' })
  })

  it('validates optional task routing while keeping old task responses unchanged', async () => {
    const taskId = 'a'.repeat(32)
    const legacy = { taskId, taskType: 'asr' }
    vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce(response(legacy))
      .mockResolvedValueOnce(response({ ...legacy, routing: taskRoutingFixture() }))
      .mockResolvedValueOnce(response({ ...legacy, routing: { selectionState: 'selected' } })))
    expect(await api.task(taskId)).toEqual(legacy)
    expect((await api.task(taskId)).routing?.selectionState).toBe('waiting')
    await expect(api.task(taskId)).rejects.toMatchObject({ code: 'INVALID_RESPONSE' })
  })
})

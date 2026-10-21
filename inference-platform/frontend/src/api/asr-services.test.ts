import { afterEach, describe, expect, it, vi } from 'vitest'
import { asrServicesApi } from './asr-services'
import { serviceFixture, servicesFixture } from '@/test/asrServicesFixtures'

const response = (data: unknown, status = 200) => new Response(JSON.stringify({ data, requestId: 'fixture' }), { status, headers: { 'Content-Type': 'application/json' } })
const config = () => { const { revision: _, ...value } = serviceFixture(); return value }
afterEach(() => vi.unstubAllGlobals())

describe('manual ASR configuration API', () => {
  it('saves to the platform only, preserving exact revision and environment-variable authentication', async () => {
    const fetchMock = vi.fn().mockResolvedValue(response(servicesFixture()))
    vi.stubGlobal('fetch', fetchMock)
    const value = { ...config(), apiKeyEnv: 'ASR_API_KEY' }
    await asrServicesApi.save(value, 'b'.repeat(64))
    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(fetchMock).toHaveBeenCalledWith('/api/asr/services/speech-main', expect.objectContaining({
      method: 'PUT', body: JSON.stringify({ config: value, expectedRevision: 'b'.repeat(64) }),
    }))
  })
  it('clears a selection using its current revision, without automatic retries', async () => {
    const fetchMock = vi.fn().mockResolvedValue(response(servicesFixture()))
    vi.stubGlobal('fetch', fetchMock)
    await asrServicesApi.select(null, 'a'.repeat(64))
    expect(fetchMock).toHaveBeenCalledWith('/api/asr/services/selection', expect.objectContaining({ method: 'POST', body: JSON.stringify({ serviceId: null, expectedRevision: 'a'.repeat(64) }) }))
  })
  it('keeps a failed health result distinct from a transport failure', async () => {
    const result = { success: false, phase: 'health', message: '服务未加载', durationMs: 23, configRevision: 'a'.repeat(64), errorCode: 'MODEL_NOT_READY', apiVersion: null }
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(response(result)))
    expect(await asrServicesApi.test(config())).toEqual(result)
  })
  it.each([null, {}, { ...servicesFixture(), items: [serviceFixture(), serviceFixture()] }, { ...servicesFixture(), restartRequired: 'yes' }])('rejects malformed or duplicate configuration state', async value => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(response(value)))
    await expect(asrServicesApi.list()).rejects.toMatchObject({ code: 'INVALID_RESPONSE' })
  })
  it('does not repeat a timed-out or uncertain save', async () => {
    const fetchMock = vi.fn().mockRejectedValue(new TypeError('offline'))
    vi.stubGlobal('fetch', fetchMock)
    await expect(asrServicesApi.save(config(), null)).rejects.toMatchObject({ code: 'NETWORK_ERROR' })
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })
})

import { afterEach, describe, expect, it, vi } from 'vitest'
import { readExternalAsrStatus } from './external-asr'

const status = { configured: false, available: false, state: 'unconfigured', observedAt: null, message: '待配置', currentTaskId: null, uncertainTaskId: null, modelId: null, modelVersion: null }
const response = (data: unknown) => new Response(JSON.stringify({ data, requestId: 'fixture' }), { headers: { 'Content-Type': 'application/json' } })
afterEach(() => vi.unstubAllGlobals())

describe('external ASR status client', () => {
  it('only reads the relative platform endpoint without management credentials', async () => {
    const fetchMock = vi.fn().mockResolvedValue(response(status))
    vi.stubGlobal('fetch', fetchMock)
    expect(await readExternalAsrStatus()).toEqual(status)
    expect(fetchMock).toHaveBeenCalledWith('/api/asr/external/status', expect.objectContaining({ headers: { Accept: 'application/json' } }))
  })
  it('retains the explicit unknown-version metadata for a usable external service', async () => {
    const value = { ...status, configured: true, available: true, state: 'ready', modelId: 'fixture', modelVersion: 'integration-fixture',
      versionInfo: { versionBasis: 'integration_revision', upstreamVersion: null } }
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(response(value)))
    expect(await readExternalAsrStatus()).toEqual(value)
  })
  it.each([null, {}, { versionBasis: 'model_version', upstreamVersion: null }, { versionBasis: 'integration_revision', upstreamVersion: 'guessed' }])('rejects ambiguous version provenance %j', async versionInfo => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(response({ ...status, configured: true, modelId: 'fixture', modelVersion: 'integration-fixture', versionInfo })))
    await expect(readExternalAsrStatus()).rejects.toMatchObject({ code: 'INVALID_RESPONSE' })
  })
  it.each([null, { ...status, available: 'yes' }, { ...status, state: 'made-up' }, { ...status, observedAt: 'invalid' }, { ...status, available: true }, { ...status, currentTaskId: 'invalid' }])('rejects an invalid status response', async value => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(response(value)))
    await expect(readExternalAsrStatus()).rejects.toMatchObject({ code: 'INVALID_RESPONSE' })
  })
})

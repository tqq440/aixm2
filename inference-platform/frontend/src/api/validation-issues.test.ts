import { afterEach, expect, it, vi } from 'vitest'
import { validationIssuesApi } from '@/api/validation-issues'

const issue = { stage: 'module6b', target: 'lab', code: 'CAMPUS_NETWORK_UNAVAILABLE', source: 'operator', state: 'waiting_external_verification', firstObservedAt: '2026-10-22T01:00:00Z', lastObservedAt: '2026-10-22T02:00:00Z', observationCount: 1, evidenceSha256: null }
const page = { items: [issue], total: 1, offset: 0, limit: 50, unreadableCount: 0, scanTruncated: false, notice: '仅本地记录' }
const reply = (value: unknown) => vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({ data: value, requestId: 'test-validation' }), { status: 200 })))
afterEach(() => vi.unstubAllGlobals())

it('uses a GET to the bounded local listing and propagates abort', async () => {
  reply(page); const controller = new AbortController()
  await validationIssuesApi.list('module6b', 0, controller.signal)
  expect(vi.mocked(fetch).mock.calls[0][0]).toBe('/api/admin/validation/issues?offset=0&limit=50&stage=module6b')
  expect(vi.mocked(fetch).mock.calls[0][1]?.method).toBeUndefined()
})

it.each([
  { items: [{ ...issue, state: 'passed' }] }, { items: [{ ...issue, code: 'FAKE_OK' }] },
  { items: [{ ...issue, target: 'redis://PRIVATE@host' }] }, { items: [{ ...issue, lastObservedAt: 'bad-date' }] },
  { total: -1 }, { offset: 20 }, { items: null }, { scanTruncated: 'false' },
])('rejects malformed state instead of inventing a safe empty list: %s', async changes => {
  reply({ ...page, ...changes })
  await expect(validationIssuesApi.list('module6b')).rejects.toMatchObject({ code: 'INVALID_RESPONSE' })
})

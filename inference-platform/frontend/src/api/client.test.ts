import { afterEach, describe, expect, it, vi } from 'vitest'
import { api, ApiError } from './client'

function json(data: unknown, status = 200, requestId = 'request-123') {
  return new Response(JSON.stringify(data), { status, headers: { 'Content-Type': 'application/json', 'X-Request-ID': requestId } })
}

afterEach(() => { vi.unstubAllGlobals(); vi.useRealTimers() })

describe('API client', () => {
  it('omits the language field for an external service controlled language', async () => {
    const fetchMock = vi.fn().mockResolvedValue(json({ data: { task: { taskId: 'a'.repeat(32) }, replayed: false }, requestId: 'request-123' }, 201))
    vi.stubGlobal('fetch', fetchMock)
    await api.submit(new File(['audio'], 'speech.wav'), 'remote-fixture', 'fixture-v1', null, 'same-key')
    const body = fetchMock.mock.calls[0][1].body as FormData
    expect(body.has('language')).toBe(false)
    expect(body.get('modelId')).toBe('remote-fixture')
  })
  it('requests the real relative API with a page size of twenty', async () => {
    const fetchMock = vi.fn().mockResolvedValue(json({ data: { items: [], total: 0, page: 2, pageSize: 20 }, requestId: 'request-123' }))
    vi.stubGlobal('fetch', fetchMock)
    expect(await api.tasks(2)).toEqual({ items: [], total: 0, page: 2, pageSize: 20 })
    expect(fetchMock).toHaveBeenCalledWith('/api/tasks?page=2&pageSize=20', expect.objectContaining({ cache: 'no-store' }))
  })

  it('uploads multipart fields and the caller-held key without hardcoding a content type', async () => {
    const fetchMock = vi.fn().mockResolvedValue(json({ data: { task: { taskId: 'a'.repeat(32) }, replayed: false }, requestId: 'request-123' }, 201))
    vi.stubGlobal('fetch', fetchMock)
    await api.submit(new File(['audio'], 'speech.wav'), 'whisper-small', 'exact-revision', 'auto', 'same-key')
    const [url, options] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(url).toBe('/api/tasks/asr')
    expect(options.method).toBe('POST')
    expect(options.headers).toEqual({ Accept: 'application/json', 'Idempotency-Key': 'same-key' })
    const body = options.body as FormData
    expect(body.get('modelId')).toBe('whisper-small')
    expect(body.get('modelVersion')).toBe('exact-revision')
    expect(body.get('language')).toBe('auto')
    expect((body.get('file') as File).name).toBe('speech.wav')
  })

  it('preserves a backend error code, message, validation details and request identifier', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(json({
      error: { code: 'VALIDATION_ERROR', message: '语言参数无效', details: [{ field: 'language', code: 'invalid' }] },
      requestId: 'server-request',
    }, 422)))
    await expect(api.models()).rejects.toMatchObject({
      code: 'VALIDATION_ERROR', message: '语言参数无效', status: 422, requestId: 'server-request',
      details: [{ field: 'language', code: 'invalid' }],
    })
  })

  it('sends an empty cancellation POST and keeps the backend decision distinct from task status', async () => {
    const receipt = { task: { taskId: 'a'.repeat(32), status: 'cancel_requested' }, decision: 'cancel_requested' }
    const fetchMock = vi.fn().mockResolvedValue(json({ data: receipt, requestId: 'request-123' }))
    vi.stubGlobal('fetch', fetchMock)
    expect(await api.cancel('a'.repeat(32))).toEqual(receipt)
    const [url, options] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(url).toBe(`/api/tasks/${'a'.repeat(32)}/cancel`)
    expect(options.method).toBe('POST')
    expect(options.body).toBeUndefined()
    expect(options.headers).toEqual({ Accept: 'application/json' })
  })

  it('reports a proxy HTML error as an unavailable API instead of showing server HTML', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('<html>proxy failure</html>', { status: 502 })))
    await expect(api.runtime()).rejects.toMatchObject({ code: 'INVALID_RESPONSE', status: 502 })
  })

  it('bounds reads with an aborting timeout and distinguishes an intentional abort', async () => {
    vi.useFakeTimers()
    const signals: AbortSignal[] = []
    vi.stubGlobal('fetch', vi.fn().mockImplementation((_url: string, options: RequestInit) => new Promise((_ok, fail) => {
      const signal = options.signal!
      signals.push(signal)
      signal.addEventListener('abort', () => fail(new DOMException('Aborted', 'AbortError')))
    })))
    const timed = api.runtime().catch(error => error as ApiError)
    await vi.advanceTimersByTimeAsync(10_000)
    expect(await timed).toMatchObject({ code: 'REQUEST_TIMEOUT' })
    expect(signals[0].aborted).toBe(true)
    const controller = new AbortController()
    const interrupted = api.runtime(controller.signal).catch(error => error as ApiError)
    controller.abort()
    expect(await interrupted).toMatchObject({ code: 'REQUEST_ABORTED' })
    expect(vi.getTimerCount()).toBe(0)
  })
})

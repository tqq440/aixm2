import { afterEach, expect, it, vi } from 'vitest'
import { api } from './client'

const id = 'b'.repeat(32)
const json = (data: unknown, status = 200) => new Response(JSON.stringify({ data, requestId: 'vision-request' }), { status, headers: { 'Content-Type': 'application/json' } })
afterEach(() => { vi.unstubAllGlobals(); vi.useRealTimers() })

it('isolates vision catalogs and pages and sends only fixed vision parameters', async () => {
  const fetcher = vi.fn().mockImplementation(() => Promise.resolve(json({ items: [] })))
  vi.stubGlobal('fetch', fetcher)
  await api.visionModels()
  await api.visionTasks(3)
  await api.submitVision(new File(['image'], 'image.png'), 'yolo-general', 'fixed-version', { confidenceThreshold: .3, iouThreshold: .5, maxDetections: 20 }, 'original-key')
  expect(fetcher.mock.calls[0][0]).toBe('/api/models?taskType=vision')
  expect(fetcher.mock.calls[1][0]).toBe('/api/tasks?page=3&pageSize=20&taskType=vision')
  const [url, init] = fetcher.mock.calls[2] as [string, RequestInit]
  expect(url).toBe('/api/tasks/vision')
  expect(init.headers).toEqual({ Accept: 'application/json', 'Idempotency-Key': 'original-key' })
  const body = init.body as FormData
  expect(body.get('modelVersion')).toBe('fixed-version')
  expect(body.get('confidenceThreshold')).toBe('0.3')
  expect(body.get('iouThreshold')).toBe('0.5')
  expect(body.get('maxDetections')).toBe('20')
  expect(body.has('language')).toBe(false)
})

it('rejects an ASR task or mismatched result instead of rendering it as an image', async () => {
  const fetcher = vi.fn().mockResolvedValueOnce(json({ taskId: id, taskType: 'asr' }))
    .mockResolvedValueOnce(json({ taskId: 'c'.repeat(32), output: { detections: [] } }))
  vi.stubGlobal('fetch', fetcher)
  await expect(api.visionTask(id)).rejects.toMatchObject({ code: 'TASK_TYPE_MISMATCH' })
  await expect(api.visionResult(id)).rejects.toMatchObject({ code: 'INVALID_RESPONSE' })
})

it('rejects a vision ID in the legacy ASR workspace without reading audio fields', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce(json({ taskId: id, taskType: 'vision' }))
    .mockResolvedValueOnce(json({ taskId: id, output: { detections: [] } })))
  await expect(api.task(id)).rejects.toMatchObject({ code: 'TASK_TYPE_MISMATCH' })
  await expect(api.result(id)).rejects.toMatchObject({ code: 'INVALID_RESPONSE' })
})

it('reads PNG bytes and preserves structured image failures and request IDs', async () => {
  const fetcher = vi.fn().mockResolvedValueOnce(new Response('PNG bytes', { headers: { 'Content-Type': 'image/png' } }))
    .mockResolvedValueOnce(new Response(JSON.stringify({ error: { code: 'INPUT_UNAVAILABLE', message: '受管图片不可读取', details: [] }, requestId: 'image-request' }), { status: 503, headers: { 'Content-Type': 'application/json' } }))
  vi.stubGlobal('fetch', fetcher)
  expect((await api.image(id, 'original')).size).toBe(9)
  expect(fetcher.mock.calls[0][0]).toBe(`/api/tasks/${id}/image?variant=original`)
  await expect(api.image(id, 'annotated')).rejects.toMatchObject({ code: 'INPUT_UNAVAILABLE', requestId: 'image-request', status: 503 })
})

it('rejects HTML or empty successful image responses', async () => {
  const fetcher = vi.fn().mockResolvedValueOnce(new Response('<html>wrong proxy</html>'))
    .mockResolvedValueOnce(new Response('', { headers: { 'Content-Type': 'image/png' } }))
  vi.stubGlobal('fetch', fetcher)
  await expect(api.image(id, 'original')).rejects.toMatchObject({ code: 'INVALID_RESPONSE' })
  await expect(api.image(id, 'annotated')).rejects.toMatchObject({ code: 'INVALID_RESPONSE' })
})

it('aborts a slow image read and releases its timeout', async () => {
  vi.useFakeTimers()
  let signal: AbortSignal | null = null
  vi.stubGlobal('fetch', vi.fn((_url, init: RequestInit) => new Promise((_resolve, reject) => {
    signal = init.signal!
    signal.addEventListener('abort', () => reject(new DOMException('abort', 'AbortError')))
  })))
  const outcome = api.image(id, 'original').catch(error => error)
  await vi.advanceTimersByTimeAsync(10_000)
  expect(await outcome).toMatchObject({ code: 'REQUEST_TIMEOUT' })
  expect(signal!.aborted).toBe(true)
  expect(vi.getTimerCount()).toBe(0)
})

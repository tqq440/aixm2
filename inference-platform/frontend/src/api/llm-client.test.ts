import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { llmApi } from './llm-client'
import { llmEvent, llmInput, onlineRecord, ONLINE_ID } from '../test/llmFixtures'
import type { LlmEvent } from '../types/llm'

const accepted = llmEvent('accepted', 1, { record: onlineRecord() })
const done = llmEvent('done', 3, { record: onlineRecord({ state: 'completed', permitState: 'released', outputCharacters: 3 }) })
const frame = (event: LlmEvent) => `event: ${event.type}\ndata: ${JSON.stringify(event)}\n\n`
const fetchMock = vi.fn()
function response(body: string, chunkSize = 4096, cancel = vi.fn()) {
  const data = new TextEncoder().encode(body)
  let position = 0
  return new Response(new ReadableStream<Uint8Array>({ pull(controller) {
    if (position >= data.length) { controller.close(); return }
    controller.enqueue(data.slice(position, position + chunkSize)); position += chunkSize
  }, cancel }), { headers: { 'Content-Type': 'text/event-stream; charset=utf-8', 'X-Request-ID': 'http-request' } })
}
beforeEach(() => { fetchMock.mockReset(); vi.stubGlobal('fetch', fetchMock) })
afterEach(() => vi.unstubAllGlobals())

describe('LLM streaming transport contract', () => {
  it('decodes split UTF-8, CRLF and comment heartbeats, preserving exact delta text and client identity', async () => {
    const delta = llmEvent('delta', 2, { text: '中🙂文' })
    fetchMock.mockResolvedValue(response(`: transport heartbeat\n\n${frame(accepted)}${frame(delta)}${frame(done)}`.replaceAll('\n', '\r\n'), 1))
    const events: LlmEvent[] = []
    await llmApi.stream(llmInput, new AbortController().signal, event => events.push(event))
    expect(events).toEqual([accepted, delta, done])
    expect(fetchMock).toHaveBeenCalledOnce()
    const [, init] = fetchMock.mock.calls[0]
    expect(JSON.parse(init.body)).toEqual(llmInput)
    expect(init.headers['Idempotency-Key']).toBeUndefined()
  })

  it('reads named heartbeats and error after partial text without treating it as successful completion', async () => {
    const error = llmEvent('error', 4, { error: { code: 'ENGINE_FAILED', message: '测试引擎中断' }, record: onlineRecord({ state: 'failed' }) })
    fetchMock.mockResolvedValue(response(frame(accepted) + frame(llmEvent('heartbeat', 2)) + frame(llmEvent('delta', 3, { text: '部分' })) + frame(error)))
    const events: LlmEvent[] = []
    await llmApi.stream(llmInput, new AbortController().signal, event => events.push(event))
    expect(events.map(event => event.type)).toEqual(['accepted', 'heartbeat', 'delta', 'error'])
  })

  it.each([
    ['sequence gap', frame(accepted) + frame(llmEvent('delta', 3, { text: 'x' }))],
    ['wrong online id', frame({ ...accepted, onlineRequestId: 'b'.repeat(32) })],
    ['wrong request id', frame({ ...accepted, requestId: 'other-http-request' })],
    ['delta before accepted', frame(llmEvent('delta', 1, { text: 'x' }))],
    ['duplicate accepted', frame(accepted) + frame({ ...accepted, sequence: 2 })],
    ['malformed JSON', 'event: accepted\ndata: {broken}\n\n'],
    ['event type mismatch', frame(accepted).replace('event: accepted', 'event: done')],
    ['nonterminal done record', frame(accepted) + frame(llmEvent('done', 2, { record: onlineRecord() }))],
    ['wrong record id', frame({ ...accepted, record: onlineRecord({ onlineRequestId: 'b'.repeat(32) }) })],
  ])('rejects %s and never reconnects or resends the request', async (_, body) => {
    fetchMock.mockResolvedValue(response(body))
    await expect(llmApi.stream(llmInput, new AbortController().signal, vi.fn())).rejects.toMatchObject({ code: 'INVALID_STREAM' })
    expect(fetchMock).toHaveBeenCalledOnce()
  })

  it.each(['', frame(accepted), frame(accepted) + frame(llmEvent('delta', 2, { text: 'partial' })), frame(accepted) + frame({ ...done, sequence: 2 }).trimEnd()])('requires an explicit complete terminal frame at EOF', async body => {
    fetchMock.mockResolvedValue(response(body))
    await expect(llmApi.stream(llmInput, new AbortController().signal, vi.fn())).rejects.toMatchObject({ code: 'STREAM_INTERRUPTED' })
    expect(fetchMock).toHaveBeenCalledOnce()
  })

  it('returns the normal JSON error before SSE admission, preserving its request id', async () => {
    fetchMock.mockResolvedValue(new Response(JSON.stringify({ error: { code: 'LLM_NOT_CONFIGURED', message: '尚未配置服务' }, requestId: 'unconfigured-request' }), { status: 503, headers: { 'Content-Type': 'application/json' } }))
    await expect(llmApi.stream(llmInput, new AbortController().signal, vi.fn())).rejects.toMatchObject({ code: 'LLM_NOT_CONFIGURED', status: 503, requestId: 'unconfigured-request' })
  })

  it('rejects a non-SSE success response and an oversized event', async () => {
    fetchMock.mockResolvedValueOnce(new Response('{}', { headers: { 'Content-Type': 'application/json' } })).mockResolvedValueOnce(response(frame(accepted) + 'data: ' + 'x'.repeat(270_000)))
    await expect(llmApi.stream(llmInput, new AbortController().signal, vi.fn())).rejects.toMatchObject({ code: 'INVALID_STREAM' })
    await expect(llmApi.stream(llmInput, new AbortController().signal, vi.fn())).rejects.toMatchObject({ code: 'STREAM_INTERRUPTED' })
  })

  it('cancels a pending stream reader when the browser aborts', async () => {
    const cancel = vi.fn(), controller = new AbortController()
    fetchMock.mockResolvedValue(new Response(new ReadableStream({ cancel }), { headers: { 'Content-Type': 'text/event-stream' } }))
    const pending = llmApi.stream(llmInput, controller.signal, vi.fn())
    await Promise.resolve(); controller.abort()
    await expect(pending).rejects.toMatchObject({ code: 'REQUEST_ABORTED' })
    expect(cancel).toHaveBeenCalledOnce()
  })

  it('uses only LLM status, models and lifecycle routes and validates fetched record identity', async () => {
    fetchMock.mockImplementation(async (url: string) => new Response(JSON.stringify({ data: url.includes('/cancel') ? { request: onlineRecord(), decision: 'cancel_requested' } : url.includes('taskType=llm') ? { items: [] } : onlineRecord(), requestId: 'read-request' }), { headers: { 'Content-Type': 'application/json' } }))
    await llmApi.status(); await llmApi.models(); await llmApi.list(2); await llmApi.get(ONLINE_ID); await llmApi.cancel(ONLINE_ID)
    expect(fetchMock.mock.calls.map(call => call[0])).toEqual(['/api/llm/status', '/api/models?taskType=llm', '/api/llm/requests?page=2&pageSize=20', `/api/llm/requests/${ONLINE_ID}`, `/api/llm/requests/${ONLINE_ID}/cancel`])
    expect(fetchMock.mock.calls[4][1].keepalive).toBe(true)
    await expect(llmApi.get('b'.repeat(32))).rejects.toMatchObject({ code: 'INVALID_STREAM' })
  })
})

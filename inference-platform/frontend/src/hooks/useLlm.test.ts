/** Lifecycle and transport substitutes; these tests do not execute a model. */
import { act, cleanup, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from '../api/client'
import { llmApi } from '../api/llm-client'
import { deferred, llmEvent, llmInput, llmModel, llmStatus, onlineRecord, ONLINE_ID } from '../test/llmFixtures'
import { readOnlineRequestId, writeWorkspaceUrl } from '../utils/workspace'
import { useLlm } from './useLlm'
import type { LlmEvent } from '../types/llm'

const { onlineRequestId: _, ...input } = llmInput
const settle = async () => { await act(async () => { await Promise.resolve() }) }
function visibility(value: 'hidden' | 'visible') { Object.defineProperty(document, 'visibilityState', { configurable: true, value }); document.dispatchEvent(new Event('visibilitychange')) }
beforeEach(() => {
  vi.useFakeTimers(); window.history.replaceState(null, '', '/?type=llm')
  Object.defineProperty(document, 'visibilityState', { configurable: true, value: 'visible' })
  vi.spyOn(crypto, 'randomUUID').mockReturnValue('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa')
  vi.spyOn(llmApi, 'status').mockResolvedValue(llmStatus)
  vi.spyOn(llmApi, 'models').mockResolvedValue([llmModel])
  vi.spyOn(llmApi, 'list').mockResolvedValue({ items: [], total: 0, page: 1, pageSize: 20 })
  vi.spyOn(llmApi, 'get').mockImplementation(async id => onlineRecord({ onlineRequestId: id, state: 'failed', permitState: 'released' }))
  vi.spyOn(llmApi, 'cancel').mockResolvedValue({ request: onlineRecord({ state: 'cancelled', cancelRequested: true, cancellationConfirmed: true, permitState: 'released' }), decision: 'cancel_requested' })
  vi.spyOn(llmApi, 'stream').mockResolvedValue()
})
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.useRealTimers() })

describe('online browser request lifecycle', () => {
  it('performs no reads while inactive and restores only lifecycle metadata from a URL', async () => {
    writeWorkspaceUrl('llm', ONLINE_ID)
    const hook = renderHook(({ active }) => useLlm(active), { initialProps: { active: false } })
    await settle(); expect(llmApi.status).not.toHaveBeenCalled()
    hook.rerender({ active: true }); await settle()
    expect(hook.result.current.record?.onlineRequestId).toBe(ONLINE_ID)
    expect(hook.result.current.output).toBeNull()
    expect(llmApi.stream).not.toHaveBeenCalled()
  })

  it('saves the generated ID before POST and concatenates only the actual received deltas', async () => {
    vi.mocked(llmApi.get).mockResolvedValue(onlineRecord({ state: 'completed', outputCharacters: 4, permitState: 'released' }))
    vi.mocked(llmApi.stream).mockImplementation(async (submitted, _signal, emit) => {
      expect(readOnlineRequestId()).toBe(ONLINE_ID)
      expect(submitted.onlineRequestId).toBe(ONLINE_ID)
      emit(llmEvent('accepted', 1, { record: onlineRecord() }))
      emit(llmEvent('delta', 2, { text: '部分' })); emit(llmEvent('delta', 3, { text: '文字' }))
      emit(llmEvent('done', 4, { record: onlineRecord({ state: 'completed', outputCharacters: 4, permitState: 'released' }) }))
    })
    const hook = renderHook(() => useLlm(true)); await settle()
    await act(async () => { await hook.result.current.start(input) })
    expect(hook.result.current.output).toEqual({ id: ONLINE_ID, text: '部分文字', complete: true, interrupted: false })
    expect(llmApi.stream).toHaveBeenCalledOnce()
    expect(hook.result.current.running).toBe(false)
  })

  it('reconciles an unknown receipt with GET and never re-POSTs on refresh or reconnection', async () => {
    vi.mocked(llmApi.stream).mockRejectedValue(new ApiError('连接断开', 'NETWORK_ERROR'))
    const hook = renderHook(() => useLlm(true)); await settle()
    await act(async () => { await hook.result.current.start(input) }); await settle()
    expect(readOnlineRequestId()).toBe(ONLINE_ID)
    expect(llmApi.get).toHaveBeenCalledWith(ONLINE_ID, expect.any(AbortSignal))
    expect(hook.result.current.streamError?.code).toBe('NETWORK_ERROR')
    act(() => hook.result.current.refresh()); await settle()
    await act(async () => { await vi.advanceTimersByTimeAsync(10_000) })
    expect(llmApi.stream).toHaveBeenCalledOnce()
  })

  it('retains partial text and failure on abrupt EOF or a terminal server error', async () => {
    vi.mocked(llmApi.stream).mockImplementation(async (_input, _signal, emit) => {
      emit(llmEvent('accepted', 1, { record: onlineRecord() })); emit(llmEvent('delta', 2, { text: '已收到的一部分' }))
      throw new ApiError('未收到终止事件', 'STREAM_INTERRUPTED')
    })
    const hook = renderHook(() => useLlm(true)); await settle()
    await act(async () => { await hook.result.current.start(input) })
    expect(hook.result.current.output?.text).toBe('已收到的一部分')
    expect(hook.result.current.output?.interrupted).toBe(true)
    expect(hook.result.current.output?.complete).toBe(false)
    expect(hook.result.current.streamError?.code).toBe('STREAM_INTERRUPTED')
  })

  it('handles a terminal error event without inventing completion or retrying', async () => {
    vi.mocked(llmApi.stream).mockImplementation(async (_input, _signal, emit) => {
      emit(llmEvent('accepted', 1, { record: onlineRecord() })); emit(llmEvent('delta', 2, { text: 'partial' }))
      emit(llmEvent('error', 3, { record: onlineRecord({ state: 'failed' }), error: { code: 'ENGINE_PROTOCOL_ERROR', message: '测试错误' } }))
    })
    const hook = renderHook(() => useLlm(true)); await settle()
    await act(async () => { await hook.result.current.start(input) })
    expect(hook.result.current.streamError?.code).toBe('ENGINE_PROTOCOL_ERROR')
    expect(hook.result.current.output?.interrupted).toBe(true)
    expect(llmApi.stream).toHaveBeenCalledOnce()
  })

  it('stops before the first accepted event using the already saved ID and ignores late deltas', async () => {
    const stream = deferred<void>(); let emit!: (event: LlmEvent) => void
    vi.mocked(llmApi.stream).mockImplementation((_input, _signal, onEvent) => { emit = onEvent; return stream.promise })
    const hook = renderHook(() => useLlm(true)); await settle()
    act(() => { void hook.result.current.start(input) })
    const signal = vi.mocked(llmApi.stream).mock.calls[0][1]
    act(() => hook.result.current.stop()); await settle()
    expect(signal.aborted).toBe(true)
    expect(llmApi.cancel).toHaveBeenCalledWith(ONLINE_ID)
    act(() => emit(llmEvent('delta', 2, { text: '迟到回答' })))
    expect(hook.result.current.output?.text).toBe('')
    await act(async () => { stream.resolve() })
  })

  it.each(['workspace', 'visibility', 'pagehide'])('aborts and best-effort cancels on %s without automatically resuming', async reason => {
    const stream = deferred<void>()
    vi.mocked(llmApi.stream).mockReturnValue(stream.promise)
    const hook = renderHook(({ active }) => useLlm(active), { initialProps: { active: true } }); await settle()
    act(() => { void hook.result.current.start(input) })
    if (reason === 'workspace') { writeWorkspaceUrl('asr', null); hook.rerender({ active: false }) }
    else if (reason === 'visibility') act(() => visibility('hidden'))
    else act(() => window.dispatchEvent(new Event('pagehide')))
    await settle()
    expect(vi.mocked(llmApi.stream).mock.calls[0][1].aborted).toBe(true)
    expect(llmApi.cancel).toHaveBeenCalledWith(ONLINE_ID)
    const count = vi.mocked(llmApi.get).mock.calls.length
    if (reason === 'visibility') { await act(async () => { await vi.advanceTimersByTimeAsync(10_000) }); expect(llmApi.get).toHaveBeenCalledTimes(count); act(() => visibility('visible')) }
    await act(async () => { stream.resolve() })
    expect(llmApi.stream).toHaveBeenCalledOnce()
  })

  it('does not confuse an unconfirmed stop request with an engine-confirmed cancellation', async () => {
    writeWorkspaceUrl('llm', ONLINE_ID)
    const pending = onlineRecord({ state: 'streaming', cancelRequested: true, cancellationConfirmed: false, permitState: 'held' })
    vi.mocked(llmApi.get).mockResolvedValue(pending)
    vi.mocked(llmApi.cancel).mockResolvedValue({ request: pending, decision: 'cancel_requested' })
    const hook = renderHook(() => useLlm(true)); await settle()
    act(() => hook.result.current.stop()); await settle()
    expect(hook.result.current.record?.state).toBe('streaming')
    expect(hook.result.current.record?.cancellationConfirmed).toBe(false)
    expect(hook.result.current.stopNote).toContain('是否确认引擎停止')
    vi.mocked(llmApi.get).mockResolvedValue(onlineRecord({ state: 'cancelled', cancelRequested: true, cancellationConfirmed: true, permitState: 'released' }))
    await act(async () => { await vi.advanceTimersByTimeAsync(2_000) })
    expect(hook.result.current.record?.state).toBe('cancelled')
  })

  it('ignores a late detail response from an old selected request and retains no body for the new request', async () => {
    writeWorkspaceUrl('llm', ONLINE_ID)
    const old = deferred<ReturnType<typeof onlineRecord>>()
    vi.mocked(llmApi.get).mockReturnValueOnce(old.promise)
    const hook = renderHook(() => useLlm(true)); await settle()
    act(() => hook.result.current.select('b'.repeat(32))); await settle()
    await act(async () => { old.resolve(onlineRecord({ state: 'completed' })) })
    expect(hook.result.current.record?.onlineRequestId).toBe('b'.repeat(32))
    expect(hook.result.current.output).toBeNull()
  })

  it.each(['asr', 'vision'] as const)('keeps its request and partial text when browser history leaves for %s', async workspace => {
    const stream = deferred<void>()
    vi.mocked(llmApi.stream).mockImplementation((_input, _signal, emit) => {
      emit(llmEvent('accepted', 1, { record: onlineRecord() }))
      emit(llmEvent('delta', 2, { text: '离开前已收到的文字' }))
      return stream.promise
    })
    const hook = renderHook(({ active }) => useLlm(active), { initialProps: { active: true } }); await settle()
    act(() => { void hook.result.current.start(input) })
    // popstate listeners run before App's changed active prop is rendered.
    act(() => {
      writeWorkspaceUrl(workspace, 'b'.repeat(32))
      window.dispatchEvent(new PopStateEvent('popstate'))
    })
    expect(hook.result.current.selectedId).toBe(ONLINE_ID)
    expect(hook.result.current.output?.text).toBe('离开前已收到的文字')
    hook.rerender({ active: false }); await settle()
    expect(vi.mocked(llmApi.stream).mock.calls[0][1].aborted).toBe(true)
    expect(llmApi.cancel).toHaveBeenCalledOnce()
    expect(llmApi.cancel).toHaveBeenCalledWith(ONLINE_ID)
    await act(async () => { stream.resolve() })
    writeWorkspaceUrl('llm', ONLINE_ID)
    hook.rerender({ active: true }); await settle()
    expect(hook.result.current.selectedId).toBe(ONLINE_ID)
    expect(hook.result.current.output).toEqual({ id: ONLINE_ID, text: '离开前已收到的文字', complete: false, interrupted: true })
    expect(llmApi.stream).toHaveBeenCalledOnce()
  })

  it('follows history to a different online request without showing the old text or accepting late deltas', async () => {
    const stream = deferred<void>(); let emit!: (event: LlmEvent) => void
    vi.mocked(llmApi.stream).mockImplementation((_input, _signal, onEvent) => {
      emit = onEvent
      emit(llmEvent('accepted', 1, { record: onlineRecord() }))
      emit(llmEvent('delta', 2, { text: '原请求的文字' }))
      return stream.promise
    })
    const hook = renderHook(() => useLlm(true)); await settle()
    act(() => { void hook.result.current.start(input) })
    const otherId = 'b'.repeat(32)
    act(() => {
      writeWorkspaceUrl('llm', otherId)
      window.dispatchEvent(new PopStateEvent('popstate'))
    }); await settle()
    expect(hook.result.current.selectedId).toBe(otherId)
    expect(hook.result.current.record?.onlineRequestId).toBe(otherId)
    expect(hook.result.current.output).toBeNull()
    expect(vi.mocked(llmApi.stream).mock.calls[0][1].aborted).toBe(true)
    expect(llmApi.cancel).toHaveBeenCalledWith(ONLINE_ID)
    act(() => emit(llmEvent('delta', 3, { text: '旧连接迟到的文字' })))
    await act(async () => { stream.resolve() })
    expect(hook.result.current.output).toBeNull()
    expect(hook.result.current.record?.onlineRequestId).toBe(otherId)
    expect(llmApi.stream).toHaveBeenCalledOnce()
  })

  it('prevents duplicate clicks from opening two streams and cleans up a stream on unmount', async () => {
    const stream = deferred<void>()
    vi.mocked(llmApi.stream).mockReturnValue(stream.promise)
    const hook = renderHook(() => useLlm(true)); await settle()
    act(() => { void hook.result.current.start(input); void hook.result.current.start(input) })
    expect(llmApi.stream).toHaveBeenCalledOnce()
    hook.unmount(); expect(vi.mocked(llmApi.stream).mock.calls[0][1].aborted).toBe(true)
    expect(llmApi.cancel).toHaveBeenCalledWith(ONLINE_ID)
    await act(async () => { stream.resolve() })
  })
})

import { useCallback, useEffect, useRef, useState } from 'react'
import { ApiError, asApiError } from '../api/client'
import { llmApi } from '../api/llm-client'
import { onlineFinished } from '../types/llm'
import type { LlmInput, LlmModelSpec, LlmStatus, OnlineRequestPage, OnlineRequestView } from '../types/llm'
import { readOnlineRequestId, readWorkspaceUrl, TASK_ID, writeWorkspaceUrl } from '../utils/workspace'

interface Output { id: string; text: string; complete: boolean; interrupted: boolean }

/** Online transport is separate from the ASR/Vision notification task channel. */
export function useLlm(active: boolean) {
  const [status, setStatus] = useState<LlmStatus | null>(null)
  const [models, setModels] = useState<LlmModelSpec[]>([])
  const [statusError, setStatusError] = useState<ApiError | null>(null)
  const [modelsError, setModelsError] = useState<ApiError | null>(null)
  const [list, setList] = useState<OnlineRequestPage | null>(null)
  const [listError, setListError] = useState<ApiError | null>(null)
  const [page, setPage] = useState(1)
  const [selectedId, setSelectedId] = useState<string | null>(() => active ? readOnlineRequestId() : null)
  const [record, setRecord] = useState<OnlineRequestView | null>(null)
  const [recordError, setRecordError] = useState<ApiError | null>(null)
  const [output, setOutput] = useState<Output | null>(null)
  const [streamError, setStreamError] = useState<ApiError | null>(null)
  const [cancelError, setCancelError] = useState<ApiError | null>(null)
  const [stopNote, setStopNote] = useState('')
  const [running, setRunning] = useState(false)
  const [cancelling, setCancelling] = useState(false)
  const [loading, setLoading] = useState(false)
  const [revision, setRevision] = useState(0)
  const alive = useRef(false), activeRef = useRef(active), selected = useRef(selectedId)
  activeRef.current = active; selected.current = selectedId
  const currentStream = useRef<{ id: string; controller: AbortController } | null>(null)
  const recordRef = useRef(record); recordRef.current = record
  const mutationVersion = useRef(0)
  const refresh = useCallback(() => setRevision(value => value + 1), [])

  const cancel = useCallback(async (id = selected.current) => {
    if (!id) return
    mutationVersion.current += 1
    if (alive.current) { setCancelling(true); setCancelError(null) }
    try {
      const result = await llmApi.cancel(id)
      if (alive.current && selected.current === id) {
        setRecord(result.request); setRecordError(null)
        setStopNote(result.decision === 'already_finished' ? '后台已确定最终状态，未重新开始生成。' : '后台已受理停止请求；是否确认引擎停止，请查看请求状态。')
      }
    } catch (error) {
      if (alive.current && selected.current === id) setCancelError(asApiError(error))
    } finally { if (alive.current && selected.current === id) { setCancelling(false); refresh() } }
  }, [refresh])

  const interrupt = useCallback((reason: string) => {
    const stream = currentStream.current
    if (!stream) return
    currentStream.current = null
    stream.controller.abort()
    if (alive.current) {
      setRunning(false); setStopNote(reason)
      setOutput(previous => previous?.id === stream.id ? { ...previous, interrupted: true } : previous)
    }
    void cancel(stream.id)
  }, [cancel])

  const select = useCallback((id: string | null, writeUrl = true) => {
    if (id && !TASK_ID.test(id)) return
    if (currentStream.current && currentStream.current.id !== id) interrupt('切换请求后在线连接已中止，并已尝试通知后台停止；不会自动续接。')
    if (selected.current !== id) {
      selected.current = id; recordRef.current = null; setSelectedId(id); setRecord(null); setRecordError(null); setStreamError(null); setCancelError(null); setCancelling(false); setStopNote('')
    }
    if (writeUrl) writeWorkspaceUrl('llm', id)
  }, [interrupt])

  useEffect(() => {
    alive.current = true
    const leave = () => interrupt('页面已离开或隐藏，在线连接已中止并尝试通知后台停止。已收到的文字可能不完整。')
    const visibility = () => { if (document.visibilityState === 'hidden') leave(); if (activeRef.current) refresh() }
    window.addEventListener('pagehide', leave)
    document.addEventListener('visibilitychange', visibility)
    return () => { leave(); alive.current = false; window.removeEventListener('pagehide', leave); document.removeEventListener('visibilitychange', visibility) }
  }, [interrupt, refresh])

  useEffect(() => {
    if (!active) { interrupt('切换工作台后在线连接已中止，并已尝试通知后台停止；回到此处不会自动续接。'); return }
    const pop = () => {
      // The URL can change before App renders the new active workspace.
      if (activeRef.current && readWorkspaceUrl().taskType === 'llm') select(readOnlineRequestId(), false)
    }
    pop(); window.addEventListener('popstate', pop)
    return () => window.removeEventListener('popstate', pop)
  }, [active, interrupt, select])

  useEffect(() => {
    if (!active || document.visibilityState === 'hidden') return
    const controller = new AbortController()
    let timer: ReturnType<typeof setTimeout> | undefined
    const current = () => !controller.signal.aborted && alive.current
    const read = async () => {
      const snapshot = mutationVersion.current
      setLoading(true)
      await Promise.allSettled([
        llmApi.status(controller.signal).then(value => { if (current()) { setStatus(value); setStatusError(null) } }).catch(error => { if (current()) setStatusError(asApiError(error)) }),
        llmApi.models(controller.signal).then(value => { if (current()) { setModels(value); setModelsError(null) } }).catch(error => { if (current()) setModelsError(asApiError(error)) }),
        llmApi.list(page, controller.signal).then(value => { if (current()) { setList(value); setListError(null) } }).catch(error => { if (current()) setListError(asApiError(error)) }),
        selectedId && !currentStream.current ? llmApi.get(selectedId, controller.signal).then(value => { if (current() && snapshot === mutationVersion.current && !currentStream.current) { setRecord(value); recordRef.current = value; setRecordError(null) } }).catch(error => { if (current() && snapshot === mutationVersion.current && !currentStream.current) setRecordError(asApiError(error)) }) : Promise.resolve(),
      ])
      if (!current()) return
      setLoading(false)
      if (currentStream.current || (recordRef.current && !onlineFinished(recordRef.current))) timer = setTimeout(() => void read(), 2_000)
    }
    void read()
    return () => { controller.abort(); clearTimeout(timer) }
  }, [active, selectedId, page, revision])

  const start = useCallback(async (input: Omit<LlmInput, 'onlineRequestId'>) => {
    if (!activeRef.current || currentStream.current || document.visibilityState === 'hidden') return
    mutationVersion.current += 1
    const id = crypto.randomUUID().replaceAll('-', '')
    const controller = new AbortController()
    const stream = { id, controller }
    currentStream.current = stream
    selected.current = id; setSelectedId(id); setRecord(null); recordRef.current = null; setRecordError(null)
    setOutput({ id, text: '', complete: false, interrupted: false }); setStreamError(null); setCancelError(null); setCancelling(false); setStopNote(''); setRunning(true)
    // Persist only the client-generated reference before sending any bytes.
    // Unknown receipt is reconciled with GET, never by replaying the POST.
    writeWorkspaceUrl('llm', id)
    const current = () => alive.current && selected.current === id && currentStream.current === stream
    try {
      await llmApi.stream({ ...input, onlineRequestId: id }, controller.signal, event => {
        if (!current()) return
        if (event.record) { setRecord(event.record); recordRef.current = event.record }
        if (event.type === 'delta') setOutput(previous => previous?.id === id ? { ...previous, text: previous.text + event.text! } : previous)
        if (event.type === 'done') setOutput(previous => previous?.id === id ? { ...previous, complete: event.record?.state === 'completed', interrupted: event.record?.state !== 'completed' } : previous)
        if (event.type === 'error') {
          setStreamError(new ApiError(event.error!.message, event.error!.code, 0, event.requestId))
          setOutput(previous => previous?.id === id ? { ...previous, interrupted: true } : previous)
        }
      })
    } catch (error) {
      if (current()) {
        setStreamError(asApiError(error))
        setOutput(previous => previous?.id === id ? { ...previous, interrupted: true } : previous)
      }
    } finally {
      if (currentStream.current === stream) currentStream.current = null
      if (alive.current && selected.current === id) { setRunning(false); refresh() }
    }
  }, [refresh])

  const stop = useCallback(() => {
    if (currentStream.current) interrupt('在线连接已中止，并已尝试通知后台停止。停止受理不等于引擎停止已确认。')
    else void cancel()
  }, [cancel, interrupt])
  return { status, models, statusError, modelsError, list, listError, page, setPage, selectedId, record, recordError,
    output: output?.id === selectedId ? output : null, streamError, cancelError, stopNote, running, cancelling, loading, refresh, select, start, stop }
}

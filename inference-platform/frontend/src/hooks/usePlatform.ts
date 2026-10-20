import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { api, ApiError, asApiError, isUncertainSubmission } from '../api/client'
import type { AnyTaskView, CancellationReceipt, ModelByType, ParametersByType, ResultByType, RuntimeView, TaskByType, TaskPage, TaskReceipt, TaskType, VisionParameters } from '../types/api'
import { readWorkspaceUrl, TASK_ID, writeWorkspaceUrl } from '../utils/workspace'

const POLL_MS = 2_000
const isFinished = (task: AnyTaskView) => ['succeeded', 'failed', 'cancelled'].includes(task.status)

interface PlatformApi<K extends TaskType> {
  models(signal?: AbortSignal): Promise<ModelByType<K>[]>
  tasks(page: number, signal?: AbortSignal): Promise<TaskPage<TaskByType<K>>>
  task(id: string, signal?: AbortSignal): Promise<TaskByType<K>>
  result(id: string, signal?: AbortSignal): Promise<ResultByType<K>>
  cancel(id: string, signal?: AbortSignal): Promise<CancellationReceipt<TaskByType<K>>>
  submit(file: File, modelId: string, version: string, parameters: ParametersByType<K>, key: string, signal?: AbortSignal): Promise<TaskReceipt<TaskByType<K>>>
}

function platformApi<K extends TaskType>(taskType: K): PlatformApi<K> {
  // This is the only capability mapping; polling and mutation state stay shared.
  return (taskType === 'vision' ? {
    models: api.visionModels, tasks: api.visionTasks, task: api.visionTask,
    result: api.visionResult, cancel: api.visionCancel, submit: api.submitVision,
  } : api) as PlatformApi<K>
}

function parameterIdentity(parameters: string | null | VisionParameters): string {
  return parameters === null ? 'null' : typeof parameters === 'string' ? parameters : JSON.stringify([
    parameters.confidenceThreshold, parameters.iouThreshold, parameters.maxDetections,
  ])
}

interface Submission<K extends TaskType> {
  file: File
  modelId: string
  modelVersion: string
  parameters: ParametersByType<K>
  parameterIdentity: string
  key: string
  receipt: TaskReceipt<TaskByType<K>> | null
}

interface Cancellation {
  pending: boolean
  error: ApiError | null
  uncertain: boolean
  decision: CancellationReceipt['decision'] | null
}

/** Owns browser requests only. Execution and task state transitions remain in the backend. */
export function usePlatform<K extends TaskType = 'asr'>(taskType: K = 'asr' as K, active = true) {
  const client = useMemo(() => platformApi(taskType), [taskType])
  const activeRef = useRef(active)
  activeRef.current = active
  const [models, setModels] = useState<ModelByType<K>[]>([])
  const [modelError, setModelError] = useState<ApiError | null>(null)
  const [runtime, setRuntime] = useState<RuntimeView | null>(null)
  const [runtimeError, setRuntimeError] = useState<ApiError | null>(null)
  const [tasks, setTasks] = useState<TaskPage<TaskByType<K>> | null>(null)
  const [tasksError, setTasksError] = useState<ApiError | null>(null)
  const [page, updatePage] = useState(1)
  const [selectedTaskId, updateSelected] = useState<string | null>(() => {
    if (!active) return null
    const workspace = readWorkspaceUrl()
    return workspace.taskType === taskType ? workspace.taskId : null
  })
  const selectedIdRef = useRef(selectedTaskId)
  const [task, setTask] = useState<TaskByType<K> | null>(null)
  const [taskError, setTaskError] = useState<ApiError | null>(null)
  const [result, setResult] = useState<ResultByType<K> | null>(null)
  const [resultError, setResultError] = useState<ApiError | null>(null)
  const [loading, setLoading] = useState(active)
  const [selectedLoading, setSelectedLoading] = useState(Boolean(selectedTaskId))
  const [revision, setRevision] = useState(0)
  const [submitting, setSubmitting] = useState(false)
  const [submissionError, setSubmissionError] = useState<ApiError | null>(null)
  const [submissionReceipt, setSubmissionReceipt] = useState<TaskReceipt<TaskByType<K>> | null>(null)
  const [submissionUncertain, setSubmissionUncertain] = useState(false)
  const submission = useRef<Submission<K> | null>(null)
  const submittingRef = useRef(false)
  const submitController = useRef<AbortController | null>(null)
  const alive = useRef(false)
  const pageInactive = useRef(document.visibilityState === 'hidden')
  const tasksRef = useRef<TaskPage<TaskByType<K>> | null>(null)
  const taskRef = useRef<TaskByType<K> | null>(null)
  const resultRef = useRef<ResultByType<K> | null>(null)
  const modelRevision = useRef(-1)
  const [cancellations, setCancellations] = useState<Record<string, Cancellation>>({})
  const cancelControllers = useRef(new Map<string, AbortController>())
  const mutationVersion = useRef(0)

  const selectTask = useCallback((taskId: string | null, writeUrl = true) => {
    if (!activeRef.current) return
    const validId = taskId && TASK_ID.test(taskId) ? taskId : null
    if (writeUrl) writeWorkspaceUrl(taskType, validId)
    // Clicking the already selected row must retain its loaded result and tab state.
    if (selectedIdRef.current === validId) return
    selectedIdRef.current = validId
    updateSelected(validId)
    taskRef.current = null
    resultRef.current = null
    setTask(null)
    setTaskError(null)
    setResult(null)
    setResultError(null)
    setSelectedLoading(Boolean(validId))
  }, [taskType])

  const setPage = useCallback((value: number) => {
    const nextPage = Math.min(1_000_000, Math.max(1, Math.floor(value) || 1))
    updatePage(nextPage)
    tasksRef.current = null
    setTasks(null)
    setTasksError(null)
  }, [])

  const refresh = useCallback(() => setRevision(value => value + 1), [])

  useEffect(() => {
    alive.current = true
    const stopMutations = () => {
      submitController.current?.abort()
      cancelControllers.current.forEach(controller => controller.abort())
    }
    const onVisibility = () => {
      pageInactive.current = document.visibilityState === 'hidden'
      if (document.visibilityState === 'hidden') stopMutations()
    }
    const onPageHide = () => { pageInactive.current = true; stopMutations() }
    const onPageShow = () => { pageInactive.current = document.visibilityState === 'hidden' }
    window.addEventListener('pagehide', onPageHide)
    window.addEventListener('pageshow', onPageShow)
    document.addEventListener('visibilitychange', onVisibility)
    return () => {
      alive.current = false
      stopMutations()
      window.removeEventListener('pagehide', onPageHide)
      window.removeEventListener('pageshow', onPageShow)
      document.removeEventListener('visibilitychange', onVisibility)
    }
  }, [])

  useEffect(() => {
    if (!active) {
      submitController.current?.abort()
      cancelControllers.current.forEach(controller => controller.abort())
      setLoading(false)
      setSelectedLoading(false)
      return
    }
    modelRevision.current = -1
    const onPopState = () => {
      if (!activeRef.current) return
      const workspace = readWorkspaceUrl()
      if (workspace.taskType === taskType) selectTask(workspace.taskId, false)
    }
    onPopState()
    window.addEventListener('popstate', onPopState)
    return () => window.removeEventListener('popstate', onPopState)
  }, [active, taskType, selectTask])

  useEffect(() => {
    if (!active) return
    let subscribed = true
    let paused = pageInactive.current || document.visibilityState === 'hidden'
    let generation = 0
    let timer: number | undefined
    let controller: AbortController | null = null
    let listHadError = false
    let detailHadError = false

    const clearTimer = () => {
      window.clearTimeout(timer)
      timer = undefined
    }

    const run = async (force = false) => {
      if (!subscribed || !activeRef.current || paused) return
      clearTimer()
      const token = ++generation
      controller?.abort()
      const cycle = new AbortController()
      controller = cycle
      const version = mutationVersion.current
      const current = () => subscribed && activeRef.current && !paused && token === generation && !cycle.signal.aborted && version === mutationVersion.current
      if (force) {
        setLoading(true)
        setSelectedLoading(Boolean(selectedTaskId))
      }

      const reads: Promise<void>[] = []
      reads.push((async () => {
        try {
          const value = await api.runtime(cycle.signal)
          if (current()) { setRuntime(value); setRuntimeError(null) }
        } catch (error) {
          if (current()) setRuntimeError(asApiError(error))
        }
      })())

      if (modelRevision.current !== revision) {
        reads.push((async () => {
          try {
            const value = await client.models(cycle.signal)
            if (current()) { setModels(value); setModelError(null); modelRevision.current = revision }
          } catch (error) {
            if (current()) { setModelError(asApiError(error)); modelRevision.current = revision }
          }
        })())
      }

      const previousPage = tasksRef.current
      if (force || !previousPage || previousPage.page !== page || listHadError || previousPage.items.some(item => !isFinished(item))) {
        reads.push((async () => {
          try {
            const value = await client.tasks(page, cycle.signal)
            if (current()) {
              tasksRef.current = value
              setTasks(value)
              setTasksError(null)
              listHadError = false
            }
          } catch (error) {
            if (current()) { setTasksError(asApiError(error)); listHadError = true }
          }
        })())
      }

      if (selectedTaskId) {
        reads.push((async () => {
          let currentTask = taskRef.current
          if (force || !currentTask || currentTask.taskId !== selectedTaskId || !isFinished(currentTask) || detailHadError) {
            try {
              currentTask = await client.task(selectedTaskId, cycle.signal)
              if (!current()) return
              taskRef.current = currentTask
              setTask(currentTask)
              setTaskError(null)
              detailHadError = false
            } catch (error) {
              if (current()) { setTaskError(asApiError(error)); detailHadError = true }
              return
            }
          }
          if (currentTask?.status === 'succeeded' && (force || resultRef.current?.taskId !== selectedTaskId)) {
            try {
              const value = await client.result(selectedTaskId, cycle.signal)
              if (current()) { resultRef.current = value; setResult(value); setResultError(null) }
            } catch (error) {
              if (current()) setResultError(asApiError(error))
            }
          }
        })())
      }

      await Promise.all(reads)
      if (!current()) return
      setLoading(false)
      setSelectedLoading(false)
      // Schedule after the previous cycle completes: a slow request never stacks polls.
      timer = window.setTimeout(() => { void run() }, POLL_MS)
    }

    const pause = () => {
      paused = true
      generation += 1
      clearTimer()
      controller?.abort()
      setLoading(false)
      setSelectedLoading(false)
    }
    const resume = () => {
      if (document.visibilityState === 'hidden') return
      paused = false
      void run(true)
    }
    const onVisibility = () => document.visibilityState === 'hidden' ? pause() : resume()
    document.addEventListener('visibilitychange', onVisibility)
    window.addEventListener('pagehide', pause)
    window.addEventListener('pageshow', resume)
    void run(true)
    return () => {
      subscribed = false
      generation += 1
      clearTimer()
      controller?.abort()
      document.removeEventListener('visibilitychange', onVisibility)
      window.removeEventListener('pagehide', pause)
      window.removeEventListener('pageshow', resume)
    }
  }, [active, client, page, selectedTaskId, revision])

  const clearSubmission = useCallback(() => {
    // The UI disables input changes while sending. Never discard a key during an active POST.
    if (submittingRef.current) return
    submission.current = null
    setSubmissionReceipt(null)
    setSubmissionError(null)
    setSubmissionUncertain(false)
  }, [])

  const cancelTask = useCallback(async (): Promise<CancellationReceipt<TaskByType<K>> | null> => {
    if (!activeRef.current || pageInactive.current) return null
    const selected = taskRef.current
    if (!selected || selected.taskId !== selectedIdRef.current || isFinished(selected) || selected.cancelAllowed === false
      || selected.status === 'cancel_requested' || cancelControllers.current.has(selected.taskId)) return null
    const taskId = selected.taskId
    const controller = new AbortController()
    cancelControllers.current.set(taskId, controller)
    setCancellations(previous => ({ ...previous, [taskId]: { pending: true, error: null, uncertain: false, decision: null } }))
    try {
      const receipt = await client.cancel(taskId, controller.signal)
      if (controller.signal.aborted) throw new ApiError('请求已中断。', 'REQUEST_ABORTED')
      if (alive.current) {
        setCancellations(previous => ({ ...previous, [taskId]: { pending: false, error: null, uncertain: false, decision: receipt.decision } }))
      }
      return receipt
    } catch (error) {
      if (alive.current) {
        const failure = asApiError(error)
        setCancellations(previous => ({ ...previous, [taskId]: {
          pending: false, error: failure, uncertain: isUncertainSubmission(failure), decision: null,
        } }))
      }
      return null
    } finally {
      cancelControllers.current.delete(taskId)
      if (alive.current) {
        // Ignore reads started before this POST settled, then ask the backend for its
        // current state. A successful cancellation request alone never means stopped.
        mutationVersion.current += 1
        refresh()
      }
    }
  }, [client, refresh])

  const submit = useCallback(async (file: File, modelId: string, modelVersion: string, parameters: ParametersByType<K>): Promise<TaskReceipt<TaskByType<K>> | null> => {
    // A synchronous guard also catches two clicks before React renders disabled buttons.
    if (submittingRef.current || !activeRef.current || pageInactive.current) return null
    let attempt = submission.current
    const identity = parameterIdentity(parameters)
    if (!attempt || attempt.file !== file || attempt.modelId !== modelId || attempt.modelVersion !== modelVersion || attempt.parameterIdentity !== identity) {
      attempt = { file, modelId, modelVersion, parameters: structuredClone(parameters), parameterIdentity: identity, key: crypto.randomUUID(), receipt: null }
      submission.current = attempt
    }
    if (attempt.receipt) return attempt.receipt
    submittingRef.current = true
    setSubmitting(true)
    setSubmissionError(null)
    const controller = new AbortController()
    submitController.current = controller
    try {
      const receipt = await client.submit(attempt.file, attempt.modelId, attempt.modelVersion, attempt.parameters, attempt.key, controller.signal)
      if (controller.signal.aborted || !activeRef.current || pageInactive.current) throw new ApiError('请求已中断。', 'REQUEST_ABORTED')
      attempt.receipt = receipt
      if (alive.current) {
        setSubmissionReceipt(receipt)
        setSubmissionUncertain(false)
        setPage(1)
        selectTask(receipt.task.taskId)
        refresh()
      }
      return receipt
    } catch (error) {
      if (alive.current) {
        const failure = asApiError(error)
        setSubmissionError(failure)
        setSubmissionUncertain(isUncertainSubmission(failure))
      }
      return null
    } finally {
      submittingRef.current = false
      if (submitController.current === controller) submitController.current = null
      if (alive.current) setSubmitting(false)
    }
  }, [client, refresh, selectTask, setPage])

  return {
    models, modelError, runtime, runtimeError, tasks, tasksError, page, setPage,
    selectedTaskId, selectTask, task, taskError, result, resultError,
    loading, selectedLoading, refresh, submitting, submit,
    submissionError, submissionReceipt, submissionUncertain, clearSubmission,
    cancelTask, cancellation: selectedTaskId ? cancellations[selectedTaskId] ?? null : null,
  }
}

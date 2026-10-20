import { useCallback, useEffect, useRef, useState } from 'react'
import { ApiError, asApiError } from '../api/client'
import { managementApi } from '../api/management'
import type { AsrInstance, ManagementAction, ManagementStatus, RegisteredNode } from '../types/management'

const POLL_MS = 2_000
const OBSERVATION_TTL_MS = 10_000

/** Browser observations and fixed commands only; Agent remains the runtime authority. */
export function useAsrManagement(active: boolean) {
  const [nodes, setNodes] = useState<RegisteredNode[]>([])
  const [instances, setInstances] = useState<AsrInstance[]>([])
  const [selectedNodeId, setNodeId] = useState<string | null>(null)
  const [selectedInstanceId, setInstanceId] = useState<string | null>(null)
  const [status, setStatus] = useState<ManagementStatus | null>(null)
  const [loading, setLoading] = useState(active)
  const [error, setError] = useState<ApiError | null>(null)
  const [commandError, setCommandError] = useState<ApiError | null>(null)
  const [commandPending, setCommandPending] = useState<ManagementAction | null>(null)
  const [commandNotice, setCommandNotice] = useState<string | null>(null)
  const [revision, setRevision] = useState(0)
  const activeRef = useRef(active)
  activeRef.current = active
  const alive = useRef(false)
  const nodeIdRef = useRef<string | null>(null)
  const instanceIdRef = useRef<string | null>(null)
  const statusRef = useRef<ManagementStatus | null>(null)
  const readController = useRef<AbortController | null>(null)
  const commandController = useRef<AbortController | null>(null)
  const readVersion = useRef(0)
  const commandVersion = useRef(0)
  const pendingRef = useRef(false)
  const observationTimer = useRef<number | undefined>(undefined)

  const forgetStatus = useCallback(() => {
    window.clearTimeout(observationTimer.current)
    statusRef.current = null; setStatus(null)
  }, [])
  const rememberStatus = useCallback((observed: ManagementStatus | null) => {
    forgetStatus()
    statusRef.current = observed; setStatus(observed); setError(null)
    if (observed) observationTimer.current = window.setTimeout(() => {
      if (!alive.current || !activeRef.current) return
      forgetStatus()
      setError(new ApiError('管理状态已过期，正在重新核对；暂不能确认模型就绪或进程退出。', 'MANAGEMENT_OBSERVATION_EXPIRED'))
    }, OBSERVATION_TTL_MS)
  }, [forgetStatus])
  const refresh = useCallback(() => {
    readVersion.current += 1
    readController.current?.abort()
    forgetStatus()
    setRevision(value => value + 1)
  }, [forgetStatus])

  const resetSelection = useCallback(() => {
    commandVersion.current += 1
    commandController.current?.abort()
    pendingRef.current = false
    setCommandPending(null); setCommandError(null); setCommandNotice(null); setError(null)
    refresh()
  }, [refresh])

  const selectNode = useCallback((value: string) => {
    if (!activeRef.current || value === nodeIdRef.current || !nodes.some(item => item.nodeId === value)) return
    nodeIdRef.current = value; instanceIdRef.current = null
    setNodeId(value); setInstanceId(null); setInstances([])
    resetSelection()
  }, [nodes, resetSelection])

  const selectInstance = useCallback((value: string) => {
    if (!activeRef.current || value === instanceIdRef.current || !instances.some(item => item.instanceId === value)) return
    instanceIdRef.current = value; setInstanceId(value)
    resetSelection()
  }, [instances, resetSelection])

  useEffect(() => {
    alive.current = true
    return () => {
      alive.current = false
      readController.current?.abort(); commandController.current?.abort()
      window.clearTimeout(observationTimer.current)
    }
  }, [])

  useEffect(() => {
    if (!active) {
      commandVersion.current += 1
      commandController.current?.abort()
      pendingRef.current = false
      setCommandPending(null); setCommandError(null); setCommandNotice(null)
      forgetStatus(); setLoading(false)
      return
    }
    let subscribed = true
    let timer: number | undefined
    let paused = document.visibilityState === 'hidden'
    const run = async () => {
      window.clearTimeout(timer)
      if (!subscribed || !activeRef.current || paused) return
      if (pendingRef.current) { timer = window.setTimeout(run, POLL_MS); return }
      const version = ++readVersion.current
      readController.current?.abort()
      const controller = new AbortController()
      readController.current = controller
      const current = () => subscribed && activeRef.current && !paused && !controller.signal.aborted && version === readVersion.current
      try {
        const fetchedNodes = await managementApi.nodes(controller.signal)
        if (!current()) return
        setNodes(fetchedNodes)
        const nodeId = fetchedNodes.find(item => item.nodeId === nodeIdRef.current)?.nodeId ?? fetchedNodes[0]?.nodeId ?? null
        if (nodeId !== nodeIdRef.current) {
          forgetStatus()
          instanceIdRef.current = null; setInstanceId(null); setInstances([])
        }
        nodeIdRef.current = nodeId; setNodeId(nodeId)
        const fetchedInstances = nodeId ? await managementApi.instances(nodeId, controller.signal) : []
        if (!current()) return
        setInstances(fetchedInstances)
        const instanceId = fetchedInstances.find(item => item.instanceId === instanceIdRef.current)?.instanceId ?? fetchedInstances[0]?.instanceId ?? null
        if (instanceId !== instanceIdRef.current) forgetStatus()
        instanceIdRef.current = instanceId; setInstanceId(instanceId)
        const observed = instanceId ? await managementApi.status(instanceId, controller.signal) : null
        if (!current()) return
        if (observed && observed.nodeId !== nodeId) throw new ApiError('后台管理状态不属于当前节点，请刷新后核对。', 'INVALID_RESPONSE', 200)
        rememberStatus(observed)
      } catch (reason) {
        if (current()) { forgetStatus(); setError(asApiError(reason)) }
      } finally {
        if (current()) setLoading(false)
        if (subscribed && activeRef.current && !paused) timer = window.setTimeout(run, POLL_MS)
      }
    }
    const pause = () => {
      paused = true
      window.clearTimeout(timer)
      readVersion.current += 1; readController.current?.abort()
      forgetStatus(); setLoading(false)
      if (pendingRef.current) commandController.current?.abort()
    }
    const resume = () => {
      paused = document.visibilityState === 'hidden'
      if (!paused) { setLoading(true); void run() }
    }
    const visibility = () => document.visibilityState === 'hidden' ? pause() : resume()
    document.addEventListener('visibilitychange', visibility)
    window.addEventListener('pagehide', pause)
    window.addEventListener('pageshow', resume)
    setLoading(!paused)
    void run()
    return () => {
      subscribed = false
      window.clearTimeout(timer); readController.current?.abort()
      document.removeEventListener('visibilitychange', visibility)
      window.removeEventListener('pagehide', pause)
      window.removeEventListener('pageshow', resume)
    }
  }, [active, revision, forgetStatus, rememberStatus])

  const command = useCallback(async (action: ManagementAction, token: string) => {
    if (!activeRef.current || !alive.current || pendingRef.current || document.visibilityState === 'hidden') return
    const observed = statusRef.current
    if (!token.trim()) { setCommandError(new ApiError('请先输入本机管理令牌。', 'MANAGEMENT_TOKEN_REQUIRED')); return }
    if (!observed?.agentAvailable || !observed.agentSessionId || observed.instanceId !== instanceIdRef.current
      || ['unknown', 'stop_unconfirmed'].includes(observed.state)) {
      setCommandError(new ApiError('当前管理状态不能确认，请先刷新并核对 Agent。', 'MANAGEMENT_STATE_UNAVAILABLE'))
      return
    }
    pendingRef.current = true
    const version = ++commandVersion.current
    const controller = new AbortController()
    commandController.current = controller
    readVersion.current += 1; readController.current?.abort()
    setCommandPending(action); setCommandError(null); setCommandNotice(null)
    // Discard pre-command observations until the Agent's response or a new GET is received.
    forgetStatus()
    const current = () => alive.current && activeRef.current && version === commandVersion.current && observed.instanceId === instanceIdRef.current
    try {
      await managementApi.command(observed.instanceId, action, {
        agentSessionId: observed.agentSessionId, expectedRevision: observed.commandRevision,
      }, token, controller.signal)
      if (current()) setCommandNotice(action === 'start'
        ? '启动命令已受理。是否可以转写，请以当前模型状态为准。'
        : '停止命令已受理。是否已经停止，请以当前状态和进程退出确认为准。')
    } catch (reason) {
      if (current()) {
        const failure = asApiError(reason)
        setCommandError(failure)
        setCommandNotice(failure.status === 409
          ? '实例或命令状态已变化，正在重新读取；核对后可手动重试，本次不会自动重发。'
          : failure.status === 0 || failure.status >= 500 || failure.code === 'INVALID_RESPONSE'
            ? '命令结果尚未确认，正在只读核对当前状态；不会自动重发，也不能据此判断进程已停止。'
            : null)
      }
    } finally {
      if (current()) {
        pendingRef.current = false; setCommandPending(null)
        refresh()
      }
    }
  }, [forgetStatus, refresh])

  return {
    nodes, instances, selectedNodeId, selectedInstanceId,
    selectedInstance: instances.find(item => item.instanceId === selectedInstanceId) ?? null,
    selectNode, selectInstance, status: active ? status : null, loading, error,
    commandError, commandPending, commandNotice, refresh,
    start: useCallback((token: string) => command('start', token), [command]),
    stop: useCallback((token: string) => command('stop', token), [command]),
  }
}

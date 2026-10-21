import { useCallback, useEffect, useRef, useState } from 'react'
import { asrServicesApi } from '@/api/asr-services'
import { asApiError, type ApiError } from '@/api/client'
import type { AsrServiceConfig, AsrServicesState } from '@/types/asrServices'

export function useAsrServices(active = true) {
  const [state, setState] = useState<AsrServicesState | null>(null)
  const [error, setError] = useState<ApiError | null>(null)
  const [actionError, setActionError] = useState<ApiError | null>(null)
  const [loading, setLoading] = useState(active)
  const [pending, setPending] = useState<string | null>(null)
  const [refreshKey, setRefreshKey] = useState(0)
  const busy = useRef(false)
  const alive = useRef(true)
  const generation = useRef(0)
  useEffect(() => { alive.current = true; return () => { alive.current = false; generation.current += 1 } }, [])
  const refresh = useCallback(() => setRefreshKey(value => value + 1), [])
  useEffect(() => {
    if (!active) { setLoading(false); return }
    const controller = new AbortController()
    const token = ++generation.current
    setLoading(true)
    void asrServicesApi.list(controller.signal).then(value => {
      if (!controller.signal.aborted && token === generation.current) { setState(value); setError(null) }
    }).catch(failure => {
      if (!controller.signal.aborted && token === generation.current) setError(asApiError(failure))
    }).finally(() => {
      if (!controller.signal.aborted && token === generation.current) setLoading(false)
    })
    return () => controller.abort()
  }, [active, refreshKey])

  const change = useCallback(async (kind: string, action: () => Promise<AsrServicesState>) => {
    if (busy.current) return null
    busy.current = true; generation.current += 1; setPending(kind); setActionError(null)
    try {
      const value = await action()
      if (alive.current) { setState(value); setError(null) }
      return value
    } catch (failure) {
      if (alive.current) setActionError(asApiError(failure))
      return null
    } finally {
      busy.current = false
      if (alive.current) { setPending(null); setLoading(false) }
    }
  }, [])
  const save = useCallback((value: AsrServiceConfig, revision: string | null) => change('save', () => asrServicesApi.save(value, revision)), [change])
  const remove = useCallback((id: string, revision: string) => change('remove', () => asrServicesApi.remove(id, revision)), [change])
  const select = useCallback((id: string | null, revision: string | null) => change('select', () => asrServicesApi.select(id, revision)), [change])
  return { state, error, actionError, loading, pending, refresh, save, remove, select }
}

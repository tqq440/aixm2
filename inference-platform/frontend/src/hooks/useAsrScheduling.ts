import { useCallback, useEffect, useState } from 'react'
import { ApiError, asApiError } from '@/api/client'
import { readAsrScheduling, readAsrTargets } from '@/api/asr-scheduling'
import type { AsrCatalogPage, AsrSchedulingStatus } from '@/types/asrScheduling'
import { snapshotFresh } from '@/utils/asrScheduling'

/** Only reads platform snapshots; never contacts or enables an upstream service. */
export function useAsrScheduling(active = true) {
  const [status, setStatus] = useState<AsrSchedulingStatus | null>(null)
  const [targets, setTargets] = useState<AsrCatalogPage | null>(null)
  const [statusError, setStatusError] = useState<ApiError | null>(null)
  const [targetsError, setTargetsError] = useState<ApiError | null>(null)
  const [loading, setLoading] = useState(active)
  const [stale, setStale] = useState(true)
  const [now, setNow] = useState(Date.now())
  const [offset, setOffset] = useState(0)
  const [revision, setRevision] = useState(0)
  const refresh = useCallback(() => setRevision(value => value + 1), [])
  const pageSize = 5
  useEffect(() => {
    if (!active) { setStale(true); setLoading(false); return }
    let alive = true
    let generation = 0
    let poll: number | undefined
    let controller: AbortController | undefined
    const clock = window.setInterval(() => setNow(Date.now()), 1_000)
    const pause = () => {
      generation += 1; controller?.abort(); window.clearTimeout(poll)
      setStale(true); setLoading(false)
    }
    const run = async () => {
      if (!alive || document.visibilityState === 'hidden') return
      const token = ++generation
      controller?.abort(); window.clearTimeout(poll)
      const cycle = new AbortController()
      controller = cycle
      setLoading(true)
      const values = await Promise.allSettled([readAsrScheduling(cycle.signal), readAsrTargets(offset, pageSize, cycle.signal)])
      if (!alive || token !== generation || cycle.signal.aborted) return
      const [mode, catalog] = values
      if (mode.status === 'fulfilled') { setStatus(mode.value); setStatusError(null) }
      else setStatusError(asApiError(mode.reason))
      if (catalog.status === 'fulfilled') { setTargets(catalog.value); setTargetsError(null) }
      else setTargetsError(asApiError(catalog.reason))
      setNow(Date.now()); setStale(false); setLoading(false)
      poll = window.setTimeout(() => { void run() }, 3_000)
    }
    const resume = () => { if (document.visibilityState !== 'hidden') void run() }
    const visibility = () => document.visibilityState === 'hidden' ? pause() : resume()
    setStale(true)
    void run()
    document.addEventListener('visibilitychange', visibility)
    window.addEventListener('pagehide', pause); window.addEventListener('pageshow', resume)
    return () => {
      alive = false; generation += 1; controller?.abort(); window.clearTimeout(poll); window.clearInterval(clock)
      document.removeEventListener('visibilitychange', visibility)
      window.removeEventListener('pagehide', pause); window.removeEventListener('pageshow', resume)
    }
  }, [active, offset, revision])
  return { status, targets, statusError, targetsError, loading, offset, pageSize, now, refresh, setOffset,
    statusTrusted: active && !stale && !statusError && snapshotFresh(status?.observedAt, now),
    targetsTrusted: active && !stale && !targetsError && targets?.offset === offset && snapshotFresh(targets?.observedAt, now) }
}

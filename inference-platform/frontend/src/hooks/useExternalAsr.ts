import { useCallback, useEffect, useState } from 'react'
import { readExternalAsrStatus } from '../api/external-asr'
import { ApiError, asApiError } from '../api/client'
import type { ExternalAsrStatus } from '../types/api'

/** Observes an external call channel, never starts or stops the remote engine. */
export function useExternalAsr(active = true) {
  const [status, setStatus] = useState<ExternalAsrStatus | null>(null)
  const [error, setError] = useState<ApiError | null>(null)
  const [loading, setLoading] = useState(active)
  const [stale, setStale] = useState(true)
  const [revision, setRevision] = useState(0)
  const refresh = useCallback(() => setRevision(value => value + 1), [])
  useEffect(() => {
    if (!active) { setStale(true); setLoading(false); return }
    let alive = true
    let generation = 0
    let poll: number | undefined
    let expiry: number | undefined
    let controller: AbortController | null = null
    const pause = () => {
      generation += 1
      window.clearTimeout(poll); window.clearTimeout(expiry)
      controller?.abort()
      setStale(true); setLoading(false)
    }
    const run = async () => {
      if (!alive || document.visibilityState === 'hidden') return
      const token = ++generation
      window.clearTimeout(poll)
      controller?.abort()
      const cycle = new AbortController()
      controller = cycle
      setLoading(true)
      const current = () => alive && token === generation && !cycle.signal.aborted
      try {
        const value = await readExternalAsrStatus(cycle.signal)
        if (!current()) return
        const age = value.observedAt ? Date.now() - Date.parse(value.observedAt) : Infinity
        const fresh = age >= -5_000 && age < 10_000
        setStatus(value); setError(null); setStale(!fresh)
        window.clearTimeout(expiry)
        if (fresh) expiry = window.setTimeout(() => { if (alive) setStale(true) }, Math.max(0, 10_000 - age))
      } catch (failure) {
        if (!current()) return
        setError(asApiError(failure)); setStale(true)
      } finally {
        if (current()) {
          setLoading(false)
          poll = window.setTimeout(() => { void run() }, 2_000)
        }
      }
    }
    const resume = () => { if (document.visibilityState !== 'hidden') void run() }
    const visibility = () => document.visibilityState === 'hidden' ? pause() : resume()
    setStale(true)
    void run()
    document.addEventListener('visibilitychange', visibility)
    window.addEventListener('pagehide', pause)
    window.addEventListener('pageshow', resume)
    return () => {
      alive = false; generation += 1
      window.clearTimeout(poll); window.clearTimeout(expiry); controller?.abort()
      document.removeEventListener('visibilitychange', visibility)
      window.removeEventListener('pagehide', pause)
      window.removeEventListener('pageshow', resume)
    }
  }, [active, revision])
  return { status, error, loading, stale, refresh }
}

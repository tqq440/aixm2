import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import type { ExternalAsrStatus } from '../types/api'
import { ExternalAsrPanel } from './ExternalAsrPanel'

const ready: ExternalAsrStatus = { configured: true, available: true, state: 'ready', observedAt: '2026-10-19T02:00:00Z',
  message: '外部调用通道已就绪', currentTaskId: null, uncertainTaskId: null, modelId: 'sensevoice-fixture', modelVersion: 'provider-v1' }

function show(status: ExternalAsrStatus) {
  return render(<ExternalAsrPanel external={{ status, error: null, loading: false, stale: false, refresh: vi.fn() }} onSelectTask={vi.fn()} />)
}

afterEach(cleanup)

it('shows unknown upstream provenance without disabling a ready service or presenting a managed start action', () => {
  show({ ...ready, modelVersion: 'integration-fixture', versionInfo: { versionBasis: 'integration_revision', upstreamVersion: null } })
  expect(screen.getByText('可以转写')).toBeTruthy()
  expect(screen.getByText(/上游模型版本未知 · 接入修订/).textContent).toContain('integration-fixture')
  expect(screen.queryByRole('button', { name: /启动|停止/ })).toBeNull()
})

it('preserves the version label for an existing known provider version', () => {
  show(ready)
  expect(screen.getByText('provider-v1').parentElement?.textContent).toContain('版本：')
  expect(screen.queryByText(/上游模型版本未知|接入修订/)).toBeNull()
})

it('does not describe the optional upstream version as an unconfigured service prerequisite', () => {
  show({ ...ready, configured: false, available: false, state: 'unconfigured', modelId: null, modelVersion: null })
  expect(screen.getByText(/上游模型版本可以暂不提供/)).toBeTruthy()
  expect(screen.queryByText(/尚未配置真实服务和模型版本/)).toBeNull()
})

it('does not advertise new submissions while a selected service revision awaits restart', () => {
  render(<ExternalAsrPanel external={{ status: ready, error: null, loading: false, stale: false, refresh: vi.fn() }} onSelectTask={vi.fn()} restartRequired />)
  expect(screen.getByText('待重启后接单')).toBeTruthy()
  expect(screen.queryByText('可以转写')).toBeNull()
  expect(screen.getByText('provider-v1')).toBeTruthy()
})

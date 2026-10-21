import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { AsrConfigurationPanel } from './AsrConfigurationPanel'
import { asrServicesApi } from '@/api/asr-services'
import { servicesFixture } from '@/test/asrServicesFixtures'
import type { useAsrServices } from '@/hooks/useAsrServices'

vi.mock('@/api/asr-services', () => ({ asrServicesApi: { test: vi.fn() } }))
const properties = (): ReturnType<typeof useAsrServices> => ({ state: servicesFixture(), error: null, actionError: null,
  loading: false, pending: null, refresh: vi.fn(), save: vi.fn().mockResolvedValue(servicesFixture()), remove: vi.fn().mockResolvedValue(servicesFixture({ items: [] })), select: vi.fn().mockResolvedValue(servicesFixture()) })
const editSaved = () => fireEvent.change(screen.getByLabelText('编辑已保存配置'), { target: { value: 'speech-main' } })

beforeEach(() => vi.mocked(asrServicesApi.test).mockResolvedValue({ success: true, phase: 'health', message: '服务健康检查通过', durationMs: 21, configRevision: 'a'.repeat(64), errorCode: null, apiVersion: '14.2' }))
afterEach(() => { cleanup(); vi.resetAllMocks() })

describe('manual ASR configuration panel', () => {
  it('only tests on explicit click, clears the result after an edit, and never equates health with transcription', async () => {
    const services = properties()
    render(<AsrConfigurationPanel services={services} canTestAudio onTestAudio={vi.fn()} />)
    editSaved()
    expect(asrServicesApi.test).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: '测试连接' }))
    await screen.findByText('连接测试通过')
    expect(screen.getByText(/尚未验证音频转写/)).toBeTruthy()
    expect(services.save).not.toHaveBeenCalled()
    expect(services.select).not.toHaveBeenCalled()
    fireEvent.change(screen.getByLabelText('服务地址'), { target: { value: 'http://127.0.0.1:8020' } })
    expect(screen.queryByText('连接测试通过')).toBeNull()
    expect((screen.getByRole('button', { name: '选用此修订' }) as HTMLButtonElement).disabled).toBe(true)
  })
  it('saves edits using their original revision and requires a separate selection', async () => {
    const services = properties()
    render(<AsrConfigurationPanel services={services} canTestAudio onTestAudio={vi.fn()} />)
    editSaved()
    fireEvent.change(screen.getByLabelText('服务名称'), { target: { value: '新的名称' } })
    fireEvent.click(screen.getByRole('button', { name: '保存配置' }))
    await waitFor(() => expect(services.save).toHaveBeenCalledWith(expect.objectContaining({ serviceId: 'speech-main', name: '新的名称' }), 'a'.repeat(64)))
    expect(services.select).not.toHaveBeenCalled()
    expect(asrServicesApi.test).not.toHaveBeenCalled()
  })
  it('distinguishes running and selected revisions and only guides an enabled current-service audio test', () => {
    const services = properties()
    services.state = servicesFixture({ selectedRevision: 'b'.repeat(64), restartRequired: true })
    const onTestAudio = vi.fn()
    const view = render(<AsrConfigurationPanel services={services} canTestAudio={false} onTestAudio={onTestAudio} />)
    expect(screen.getByText('待重启生效')).toBeTruthy()
    expect(screen.getByText('a'.repeat(64))).toBeTruthy()
    expect(screen.getByText('b'.repeat(64))).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: '用当前服务测试音频' }))
    expect(onTestAudio).not.toHaveBeenCalled()
    view.rerender(<AsrConfigurationPanel services={{ ...services, state: servicesFixture() }} canTestAudio onTestAudio={onTestAudio} />)
    fireEvent.click(screen.getByRole('button', { name: '用当前服务测试音频' }))
    expect(onTestAudio).toHaveBeenCalledTimes(1)
    expect(asrServicesApi.test).not.toHaveBeenCalled()
  })
  it('requires an explicit delete action and preserves its revision', async () => {
    const services = properties()
    render(<AsrConfigurationPanel services={services} canTestAudio onTestAudio={vi.fn()} />)
    editSaved()
    fireEvent.click(screen.getByRole('button', { name: /^删除配置$/ }))
    expect(services.remove).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: '确认删除配置' }))
    await waitFor(() => expect(services.remove).toHaveBeenCalledWith('speech-main', 'a'.repeat(64)))
  })
  it('switches protocol defaults without inventing an upstream model or sending any request', () => {
    render(<AsrConfigurationPanel services={properties()} canTestAudio={false} onTestAudio={vi.fn()} />)
    fireEvent.change(screen.getByLabelText('接口协议'), { target: { value: 'openai-audio' } })
    expect((screen.getByLabelText('转写接口路径') as HTMLInputElement).value).toBe('/v1/audio/transcriptions')
    expect((screen.getByLabelText('健康检查路径') as HTMLInputElement).value).toBe('/v1/models')
    expect((screen.getByLabelText('接口 model 参数') as HTMLInputElement).value).toBe('')
    expect(asrServicesApi.test).not.toHaveBeenCalled()
  })
})

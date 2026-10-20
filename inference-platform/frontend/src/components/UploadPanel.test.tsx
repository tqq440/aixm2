import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { ModelSpec } from '../types/api'
import { UploadPanel } from './UploadPanel'

const original: ModelSpec = {
  modelId: 'whisper-small', modelVersion: 'original-revision', modelRepo: 'Systran/faster-whisper-small',
  taskType: 'asr', executionMode: 'offline', adapterKey: 'faster-whisper',
  parameterSchema: { defaultLanguage: 'zh', autoDetectionValue: 'auto', languages: ['zh', 'en'] },
  inputConstraints: { maxFileBytes: 20 * 1024 * 1024, maxAudioSeconds: 300 },
  outputCapabilities: ['text', 'segments', 'language'],
}
const next = { ...original, modelVersion: 'next-revision' }
const defaults = { models: [original], modelError: null, submitting: false, error: null, receipt: null, uncertain: false }

function chooseFile() {
  const file = new File(['audio'], 'speech.wav')
  fireEvent.change(screen.getByLabelText('选择音频'), { target: { files: [file] } })
  return file
}

afterEach(cleanup)

describe('upload model versions', () => {
  it('labels an unknown upstream version separately and submits the immutable integration revision', () => {
    const remote: ModelSpec = { ...original, adapterKey: 'remote-v2-sensevoice', modelId: 'remote-fixture', modelRepo: null,
      modelVersion: 'integration-fixture', versionInfo: { versionBasis: 'integration_revision', upstreamVersion: null } }
    const onSubmit = vi.fn().mockResolvedValue(null)
    render(<UploadPanel {...defaults} models={[remote]} onSubmit={onSubmit} onReset={vi.fn()} />)
    const file = chooseFile()
    expect(screen.getByText(/上游模型版本未知 · 接入修订/).textContent).toContain(remote.modelVersion)
    fireEvent.click(screen.getByRole('button', { name: '提交转写' }))
    expect(onSubmit).toHaveBeenCalledWith(file, remote.modelId, remote.modelVersion, null)
    expect(screen.getByText(/上游模型版本未知 · 本次提交接入修订/)).toBeTruthy()
  })

  it('retains the original integration revision and its meaning after a lost upload response', () => {
    const remote: ModelSpec = { ...original, adapterKey: 'remote-v2-sensevoice', modelId: 'remote-fixture',
      modelVersion: 'integration-original', versionInfo: { versionBasis: 'integration_revision', upstreamVersion: null } }
    const onSubmit = vi.fn().mockResolvedValue(null)
    const view = render(<UploadPanel {...defaults} models={[remote]} onSubmit={onSubmit} onReset={vi.fn()} />)
    const file = chooseFile()
    fireEvent.click(screen.getByRole('button', { name: '提交转写' }))
    view.rerender(<UploadPanel {...defaults} models={[{ ...remote, modelVersion: 'integration-next' }]} uncertain error={{ message: '响应丢失' }} onSubmit={onSubmit} onReset={vi.fn()} />)
    expect(screen.getByText(/仍可使用原接入修订和提交编号重试确认/)).toBeTruthy()
    expect(screen.getByText(/上游模型版本未知 · 本次提交接入修订/).textContent).toContain(remote.modelVersion)
    fireEvent.click(screen.getByRole('button', { name: '重试提交' }))
    expect(onSubmit).toHaveBeenLastCalledWith(file, remote.modelId, remote.modelVersion, null)
  })

  it('does not apply the local gate or language to the external adapter', () => {
    const remote = { ...original, adapterKey: 'remote-v2-sensevoice', modelId: 'remote-fixture', modelRepo: null }
    const onSubmit = vi.fn().mockResolvedValue(null)
    render(<UploadPanel {...defaults} models={[remote]} serviceBlock="服务未启动" serviceModel={original} externalBlock={null} onSubmit={onSubmit} onReset={vi.fn()} />)
    const file = chooseFile()
    expect(screen.queryByLabelText('识别语言')).toBeNull()
    expect(screen.getByText(/音频会发送到后台配置的外部 ASR 服务/)).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: '提交转写' }))
    expect(onSubmit).toHaveBeenCalledWith(file, remote.modelId, remote.modelVersion, null)
  })

  it('blocks unavailable external uploads but still replays an unconfirmed original submission', () => {
    const remote = { ...original, adapterKey: 'remote-v2-sensevoice', modelId: 'remote-fixture' }
    const onSubmit = vi.fn().mockResolvedValue(null)
    const view = render(<UploadPanel {...defaults} models={[remote]} externalBlock="远端结果未知" onSubmit={onSubmit} onReset={vi.fn()} />)
    const file = chooseFile()
    expect((screen.getByRole('button', { name: '提交转写' }) as HTMLButtonElement).disabled).toBe(true)
    view.rerender(<UploadPanel {...defaults} models={[remote]} externalBlock={null} onSubmit={onSubmit} onReset={vi.fn()} />)
    fireEvent.click(screen.getByRole('button', { name: '提交转写' }))
    view.rerender(<UploadPanel {...defaults} models={[]} uncertain error={{ message: '上传响应丢失' }} externalBlock="远端结果未知" onSubmit={onSubmit} onReset={vi.fn()} />)
    fireEvent.click(screen.getByRole('button', { name: '重试提交' }))
    expect(onSubmit).toHaveBeenCalledTimes(2)
    expect(onSubmit).toHaveBeenLastCalledWith(file, remote.modelId, remote.modelVersion, null)
  })
  it('keeps a selected file but blocks a new task while the service is stopped', () => {
    const onSubmit = vi.fn().mockResolvedValue(null)
    const view = render(<UploadPanel {...defaults} serviceBlock="服务未启动" onSubmit={onSubmit} onReset={vi.fn()} />)
    const file = chooseFile()
    fireEvent.submit(screen.getByRole('button', { name: '提交转写' }).closest('form')!)
    expect(onSubmit).not.toHaveBeenCalled()
    expect(screen.getByText('speech.wav')).toBeTruthy()
    view.rerender(<UploadPanel {...defaults} serviceBlock={null} onSubmit={onSubmit} onReset={vi.fn()} />)
    fireEvent.click(screen.getByRole('button', { name: '提交转写' }))
    expect(onSubmit).toHaveBeenCalledWith(file, original.modelId, original.modelVersion, 'zh')
  })

  it('allows an unconfirmed original submission to be replayed after stopping', () => {
    const onSubmit = vi.fn().mockResolvedValue(null)
    const view = render(<UploadPanel {...defaults} onSubmit={onSubmit} onReset={vi.fn()} />)
    const file = chooseFile()
    fireEvent.click(screen.getByRole('button', { name: '提交转写' }))
    view.rerender(<UploadPanel {...defaults} models={[next]} uncertain error={{ message: '响应丢失' }} serviceBlock="停止未确认" serviceModel={next} onSubmit={onSubmit} onReset={vi.fn()} />)
    fireEvent.click(screen.getByRole('button', { name: '重试提交' }))
    expect(onSubmit).toHaveBeenCalledTimes(2)
    expect(onSubmit).toHaveBeenLastCalledWith(file, original.modelId, original.modelVersion, 'zh')
  })

  it('blocks new tasks for a different version than the managed instance', () => {
    const onSubmit = vi.fn().mockResolvedValue(null)
    render(<UploadPanel {...defaults} serviceModel={next} onSubmit={onSubmit} onReset={vi.fn()} />)
    chooseFile()
    fireEvent.submit(screen.getByRole('button', { name: '提交转写' }).closest('form')!)
    expect(onSubmit).not.toHaveBeenCalled()
    expect(screen.getByText(/所选模型版本与当前 ASR 实例不同/)).toBeTruthy()
  })

  it('shows the exact catalog version and includes it in the submitted request', () => {
    const onSubmit = vi.fn().mockResolvedValue(null)
    render(<UploadPanel {...defaults} onSubmit={onSubmit} onReset={vi.fn()} />)
    const file = chooseFile()
    expect(screen.getByText('original-revision').tagName).toBe('CODE')
    expect(screen.queryByText(/上游模型版本未知/)).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: '提交转写' }))
    expect(onSubmit).toHaveBeenCalledWith(file, 'whisper-small', 'original-revision', 'zh')
  })

  it('disables new submissions when all models are disabled', () => {
    const onSubmit = vi.fn().mockResolvedValue(null)
    render(<UploadPanel {...defaults} models={[]} onSubmit={onSubmit} onReset={vi.fn()} />)
    chooseFile()
    const button = screen.getByRole('button', { name: '提交转写' }) as HTMLButtonElement
    expect(button.disabled).toBe(true)
    expect(screen.getByRole('status').textContent).toContain('已有任务仍可查看和下载')
    fireEvent.submit(button.closest('form')!)
    expect(onSubmit).not.toHaveBeenCalled()
  })

  it.each([{ models: [next] }, { models: [] }])('preserves an unconfirmed version when the refreshed catalog is $models', ({ models }) => {
    const onSubmit = vi.fn().mockResolvedValue(null)
    const onReset = vi.fn()
    const view = render(<UploadPanel {...defaults} onSubmit={onSubmit} onReset={onReset} />)
    const file = chooseFile()
    fireEvent.click(screen.getByRole('button', { name: '提交转写' }))
    view.rerender(<UploadPanel {...defaults} models={models} uncertain error={{ message: '连接中断' }} onSubmit={onSubmit} onReset={onReset} />)
    expect(screen.getByText('original-revision')).toBeTruthy()
    expect(screen.getByText(/仍可使用原版本和提交编号重试确认/)).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: '重试提交' }))
    expect(onSubmit).toHaveBeenLastCalledWith(file, 'whisper-small', 'original-revision', 'zh')
    expect(onReset).toHaveBeenCalledTimes(1) // Only the deliberate initial file choice reset identity.
  })

  it('uses the newly selected version only after the user deliberately changes the model', () => {
    const onSubmit = vi.fn().mockResolvedValue(null)
    const onReset = vi.fn()
    const view = render(<UploadPanel {...defaults} onSubmit={onSubmit} onReset={onReset} />)
    const file = chooseFile()
    fireEvent.click(screen.getByRole('button', { name: '提交转写' }))
    view.rerender(<UploadPanel {...defaults} models={[next]} uncertain error={{ message: '连接中断' }} onSubmit={onSubmit} onReset={onReset} />)
    fireEvent.change(screen.getByLabelText('模型'), { target: { value: JSON.stringify([next.modelId, next.modelVersion]) } })
    expect(screen.queryByText('original-revision')).toBeNull()
    expect(screen.getByText('next-revision')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: '重试提交' }))
    expect(onSubmit).toHaveBeenLastCalledWith(file, 'whisper-small', 'next-revision', 'zh')
    expect(onReset).toHaveBeenCalledTimes(2)
  })

  it('blocks a stale catalog after a read error but retains a safe pending replay', () => {
    const onSubmit = vi.fn().mockResolvedValue(null)
    const onReset = vi.fn()
    const view = render(<UploadPanel {...defaults} modelError={{ message: '目录读取失败' }} onSubmit={onSubmit} onReset={onReset} />)
    chooseFile()
    expect((screen.getByRole('button', { name: '提交转写' }) as HTMLButtonElement).disabled).toBe(true)
    view.rerender(<UploadPanel {...defaults} onSubmit={onSubmit} onReset={onReset} />)
    fireEvent.click(screen.getByRole('button', { name: '提交转写' }))
    view.rerender(<UploadPanel {...defaults} modelError={{ message: '目录读取失败' }} uncertain error={{ message: '响应丢失' }} onSubmit={onSubmit} onReset={onReset} />)
    expect((screen.getByRole('button', { name: '重试提交' }) as HTMLButtonElement).disabled).toBe(false)
  })
})

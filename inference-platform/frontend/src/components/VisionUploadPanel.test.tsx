import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { VisionModelSpec } from '../types/api'
import { VisionUploadPanel } from './VisionUploadPanel'

const model: VisionModelSpec = {
  modelId: 'yolo-general', modelVersion: 'yolov5n-v7.0-onnx', modelRepo: 'public/yolo', taskType: 'vision',
  executionMode: 'offline', adapterKey: 'yolov5-onnx', outputCapabilities: ['detections'],
  parameterSchema: { defaultConfidenceThreshold: 0.25, defaultIouThreshold: 0.45, defaultMaxDetections: 100, maxDetections: 300 },
  inputConstraints: { maxFileBytes: 10 * 1024 * 1024, maxPixels: 20_000_000, formats: ['JPEG', 'PNG'] },
}
const defaults = { models: [model], modelError: null, submitting: false, error: null, receipt: null, uncertain: false }
const png = () => new File([new Uint8Array([137, 80, 78, 71, 13, 10, 26, 10, 0])], 'image.png', { type: 'image/png' })
const choose = (file = png()) => { fireEvent.change(screen.getByLabelText('选择图片'), { target: { files: [file] } }); return file }
const ready = async () => { await waitFor(() => expect((screen.getByRole('button', { name: '提交检测' }) as HTMLButtonElement).disabled).toBe(false)) }
const createUrl = vi.fn()
const revokeUrl = vi.fn()
beforeEach(() => {
  let index = 0
  createUrl.mockReset().mockImplementation(() => `blob:preview-${++index}`); revokeUrl.mockReset()
  vi.stubGlobal('URL', { createObjectURL: createUrl, revokeObjectURL: revokeUrl })
})
afterEach(() => { cleanup(); vi.unstubAllGlobals() })

describe('vision upload', () => {
  it.each([
    ['PNG', png], ['JPEG', () => new File([new Uint8Array([255, 216, 255, 225, 0, 0, 0, 0])], 'camera.jpg', { type: '' })],
  ])('submits %s with catalog defaults and its exact version', async (_, makeFile) => {
    const onSubmit = vi.fn().mockResolvedValue(null)
    render(<VisionUploadPanel {...defaults} onSubmit={onSubmit} onReset={vi.fn()} />)
    const file = choose(makeFile())
    await ready()
    expect(screen.getByText(/20,000,000 像素/)).toBeTruthy()
    expect(screen.getByAltText('待提交图片预览')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: '提交检测' }))
    expect(onSubmit).toHaveBeenCalledWith(file, model.modelId, model.modelVersion, { confidenceThreshold: 0.25, iouThreshold: 0.45, maxDetections: 100 })
  })

  it('rejects an empty file, multiple files, forged image extension and a dynamic byte limit', async () => {
    const onSubmit = vi.fn()
    const view = render(<VisionUploadPanel {...defaults} onSubmit={onSubmit} onReset={vi.fn()} />)
    choose(new File([], 'empty.png')); expect(screen.getByRole('alert').textContent).toContain('空的')
    fireEvent.change(screen.getByLabelText('选择图片'), { target: { files: [png(), png()] } })
    expect(screen.getByRole('alert').textContent).toContain('一张图片')
    expect(screen.queryByText('image.png')).toBeNull()
    choose(new File(['not a real png'], 'fake.png', { type: 'image/png' }))
    await waitFor(() => expect(screen.getByRole('alert').textContent).toContain('文件内容'))
    view.rerender(<VisionUploadPanel {...defaults} models={[{ ...model, inputConstraints: { ...model.inputConstraints, maxFileBytes: 5 } }]} onSubmit={onSubmit} onReset={vi.fn()} />)
    choose()
    expect(screen.getByRole('alert').textContent).toContain('上限')
    fireEvent.submit(screen.getByRole('button', { name: '提交检测' }).closest('form')!)
    expect(onSubmit).not.toHaveBeenCalled()
  })

  it('does not create previews for an empty or over-limit file, and clears a preview when the catalog lowers its limit', async () => {
    const onSubmit = vi.fn(), onReset = vi.fn()
    const view = render(<VisionUploadPanel {...defaults} onSubmit={onSubmit} onReset={onReset} />)
    choose(new File([], 'empty.png'))
    expect(createUrl).not.toHaveBeenCalled()
    const oversized = png(); Object.defineProperty(oversized, 'size', { value: model.inputConstraints.maxFileBytes + 1 })
    choose(oversized)
    expect(createUrl).not.toHaveBeenCalled()
    expect(screen.queryByAltText('待提交图片预览')).toBeNull()
    choose(); await ready()
    expect(createUrl).toHaveBeenCalledOnce()
    view.rerender(<VisionUploadPanel {...defaults} models={[{ ...model, inputConstraints: { ...model.inputConstraints, maxFileBytes: 5 } }]} onSubmit={onSubmit} onReset={onReset} />)
    expect(screen.queryByAltText('待提交图片预览')).toBeNull()
    expect(revokeUrl).toHaveBeenCalledWith('blob:preview-1')
    expect(createUrl).toHaveBeenCalledOnce()
  })

  it.each([
    ['置信度阈值', '0', '置信度阈值必须'], ['置信度阈值', '1.1', '置信度阈值必须'],
    ['重叠阈值（IoU）', '', '重叠阈值必须'], ['最多目标数', '1.5', '最多目标数必须'], ['最多目标数', '301', '最多目标数必须'],
  ])('rejects %s=%s before submission', async (label, value, message) => {
    const onSubmit = vi.fn()
    render(<VisionUploadPanel {...defaults} onSubmit={onSubmit} onReset={vi.fn()} />)
    choose(); await ready()
    fireEvent.change(screen.getByLabelText(label), { target: { value } })
    expect(screen.getByRole('alert').textContent).toContain(message)
    fireEvent.submit(screen.getByRole('button', { name: '提交检测' }).closest('form')!)
    expect(onSubmit).not.toHaveBeenCalled()
  })

  it.each([{ models: [{ ...model, modelVersion: 'new-default', parameterSchema: { ...model.parameterSchema, defaultConfidenceThreshold: 0.8 } }] }, { models: [] }])('keeps the original version and custom parameters through uncertain catalog refresh', async ({ models }) => {
    const onSubmit = vi.fn().mockResolvedValue(null), onReset = vi.fn()
    const view = render(<VisionUploadPanel {...defaults} onSubmit={onSubmit} onReset={onReset} />)
    const file = choose(); await ready()
    fireEvent.change(screen.getByLabelText('置信度阈值'), { target: { value: '0.31' } })
    fireEvent.change(screen.getByLabelText('重叠阈值（IoU）'), { target: { value: '0.6' } })
    fireEvent.change(screen.getByLabelText('最多目标数'), { target: { value: '8' } })
    fireEvent.click(screen.getByRole('button', { name: '提交检测' }))
    const resetCount = onReset.mock.calls.length
    view.rerender(<VisionUploadPanel {...defaults} models={models} uncertain error={{ message: '响应未确认' }} modelError={{ message: '目录暂不可用' }} onSubmit={onSubmit} onReset={onReset} />)
    expect(screen.getByText(model.modelVersion)).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: '重试提交' }))
    expect(onSubmit).toHaveBeenLastCalledWith(file, model.modelId, model.modelVersion, { confidenceThreshold: 0.31, iouThreshold: 0.6, maxDetections: 8 })
    expect(onReset).toHaveBeenCalledTimes(resetCount)
  })

  it('uses selected catalog defaults and rejects stale or unavailable catalogs for a new request', async () => {
    const onSubmit = vi.fn(), onReset = vi.fn()
    const next = { ...model, modelVersion: 'new-default', parameterSchema: { ...model.parameterSchema, defaultConfidenceThreshold: 0.6, defaultMaxDetections: 12, maxDetections: 20 } }
    const view = render(<VisionUploadPanel {...defaults} models={[model, next]} onSubmit={onSubmit} onReset={onReset} />)
    choose(); await ready()
    fireEvent.change(screen.getByLabelText('检测模型'), { target: { value: JSON.stringify([next.modelId, next.modelVersion]) } })
    expect((screen.getByLabelText('置信度阈值') as HTMLInputElement).value).toBe('0.6')
    expect((screen.getByLabelText('最多目标数') as HTMLInputElement).value).toBe('12')
    view.rerender(<VisionUploadPanel {...defaults} modelError={{ message: '连接失败' }} onSubmit={onSubmit} onReset={onReset} />)
    expect((screen.getByRole('button', { name: '提交检测' }) as HTMLButtonElement).disabled).toBe(true)
    view.rerender(<VisionUploadPanel {...defaults} models={[]} onSubmit={onSubmit} onReset={onReset} />)
    expect((screen.getByRole('button', { name: '提交检测' }) as HTMLButtonElement).disabled).toBe(true)
    expect(screen.getByText(/已有任务仍可查看和下载/)).toBeTruthy()
  })

  it('supports dropping one file and releases previews on replacement, removal and unmount', async () => {
    const view = render(<VisionUploadPanel {...defaults} onSubmit={vi.fn()} onReset={vi.fn()} />)
    fireEvent.drop(screen.getByText('拖拽图片到这里').closest('label')!, { dataTransfer: { files: [png()] } })
    await ready(); expect(createUrl).toHaveBeenCalledTimes(1)
    choose(); await waitFor(() => expect(createUrl).toHaveBeenCalledTimes(2))
    expect(revokeUrl).toHaveBeenCalledWith('blob:preview-1')
    fireEvent.click(screen.getByRole('button', { name: '移除图片' }))
    expect(revokeUrl).toHaveBeenCalledWith('blob:preview-2')
    expect(screen.queryByAltText('待提交图片预览')).toBeNull()
    choose(); await ready(); view.unmount()
    expect(revokeUrl).toHaveBeenCalledWith('blob:preview-3')
  })

  it('keeps browser preview failure separate from authoritative decoding and freezes fields during upload', async () => {
    const onSubmit = vi.fn()
    const view = render(<VisionUploadPanel {...defaults} onSubmit={onSubmit} onReset={vi.fn()} />)
    choose(); await ready()
    fireEvent.error(screen.getByAltText('待提交图片预览'))
    expect(screen.getByText(/由后台检查是否能够解码/)).toBeTruthy()
    expect((screen.getByRole('button', { name: '提交检测' }) as HTMLButtonElement).disabled).toBe(false)
    view.rerender(<VisionUploadPanel {...defaults} submitting onSubmit={onSubmit} onReset={vi.fn()} />)
    expect((screen.getByLabelText('选择图片') as HTMLInputElement).disabled).toBe(true)
    expect((screen.getByLabelText('置信度阈值') as HTMLInputElement).disabled).toBe(true)
    expect((screen.getByRole('button', { name: '正在上传并提交…' }) as HTMLButtonElement).disabled).toBe(true)
  })
})

import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { api, ApiError } from '../api/client'
import type { DetectionResult, TaskResultView, VisionTaskView } from '../types/api'
import * as format from '../utils/format'
import { VisionTaskDetail } from './VisionTaskDetail'

const task: VisionTaskView = {
  taskId: 'a'.repeat(32), callerId: 'local-user', taskType: 'vision', modelId: 'yolo-general', modelVersion: 'yolov5n-v7.0-onnx',
  parameters: { confidenceThreshold: 0.25, iouThreshold: 0.45, maxDetections: 100 }, status: 'succeeded', attempt: 1, maxAttempts: 2, executionPlanVersion: 1,
  input: { fileName: 'bus.jpg', sizeBytes: 20_000, sha256: 'f'.repeat(64), mediaType: 'image', width: 810, height: 1080 },
  createdAt: '2026-10-13T04:00:00Z', updatedAt: '2026-10-13T04:00:02Z', startedAt: '2026-10-13T04:00:01Z', finishedAt: '2026-10-13T04:00:02Z',
  queueDeadlineAt: '2026-10-13T04:10:00Z', totalDeadlineAt: '2026-10-13T04:30:00Z', attemptDeadlineAt: '2026-10-13T04:15:01Z', nextRetryAt: null,
  error: null, resultAvailable: true, message: '图片检测完成，结果已保存。',
}
const result: TaskResultView<DetectionResult> = {
  taskId: task.taskId, modelId: task.modelId, modelVersion: task.modelVersion, schemaVersion: '1',
  output: {
    modelId: task.modelId, modelVersion: task.modelVersion, modelSha256: '1'.repeat(64), imageWidth: 810, imageHeight: 1080,
    coordinateSystem: 'original-pixels-exif-normalized', device: 'cpu', runtimeVersion: '1.31.0', inputType: 'float16', preprocessingVersion: 'pillow-bilinear-letterbox-v1',
    loadSeconds: 0.8, inferenceSeconds: 0.03, totalSeconds: 1.2,
    detections: [{ classId: 5, className: 'bus', confidence: 0.91345678, box: { xMin: 16.12345678, yMin: 202.5, xMax: 750.1, yMax: 850.9 } }],
  },
}
const defaults = { task, result, selectedId: task.taskId, taskError: null, resultError: null, loading: false, onRefresh: vi.fn(), onCancel: vi.fn().mockResolvedValue(null), cancellation: null }
function deferred<T>() { let resolve!: (value: T) => void; let reject!: (reason: unknown) => void; const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no }); return { promise, resolve, reject } }
const imageBlob = new Blob(['png'], { type: 'image/png' })
const createUrl = vi.fn(), revokeUrl = vi.fn()
beforeEach(() => {
  let index = 0
  createUrl.mockReset().mockImplementation(() => `blob:image-${++index}`); revokeUrl.mockReset()
  vi.stubGlobal('URL', { createObjectURL: createUrl, revokeObjectURL: revokeUrl })
  vi.spyOn(api, 'image').mockResolvedValue(imageBlob)
  vi.spyOn(format, 'downloadBlob').mockImplementation(() => {})
  vi.spyOn(format, 'downloadText').mockImplementation(() => {})
})
afterEach(() => { cleanup(); vi.unstubAllGlobals() })

describe('vision task images and detection output', () => {
  it('loads the normalized original and switches to the backend annotation without retaining the old image', async () => {
    render(<VisionTaskDetail {...defaults} />)
    expect(await screen.findByAltText('任务原图')).toBeTruthy()
    expect(api.image).toHaveBeenLastCalledWith(task.taskId, 'original', expect.any(AbortSignal))
    expect(screen.getByRole('table').textContent).toContain('bus / 5')
    expect(screen.getByRole('table').textContent).toContain('91.35%')
    expect(screen.getByRole('table').textContent).toContain('16.12')
    fireEvent.click(screen.getByRole('button', { name: '检测框图' }))
    expect(screen.queryByAltText('任务原图')).toBeNull()
    expect(await screen.findByAltText('目标检测框图')).toBeTruthy()
    expect(api.image).toHaveBeenLastCalledWith(task.taskId, 'annotated', expect.any(AbortSignal))
    expect(revokeUrl).toHaveBeenCalledWith('blob:image-1')
  })

  it('ignores a late original response after switching to the annotated image', async () => {
    const original = deferred<Blob>(), annotated = deferred<Blob>()
    vi.mocked(api.image).mockReturnValueOnce(original.promise).mockReturnValueOnce(annotated.promise)
    render(<VisionTaskDetail {...defaults} />)
    const signal = vi.mocked(api.image).mock.calls[0][2]!
    fireEvent.click(screen.getByRole('button', { name: '检测框图' }))
    expect(signal.aborted).toBe(true)
    await act(async () => { annotated.resolve(imageBlob) })
    const source = screen.getByAltText('目标检测框图').getAttribute('src')
    await act(async () => { original.resolve(new Blob(['late original'])) })
    expect(screen.getByAltText('目标检测框图').getAttribute('src')).toBe(source)
    expect(createUrl).toHaveBeenCalledTimes(1)
  })

  it('aborts on task change, hides stale task data and ignores a late response', async () => {
    const old = deferred<Blob>()
    vi.mocked(api.image).mockReturnValueOnce(old.promise)
    const view = render(<VisionTaskDetail {...defaults} />)
    const signal = vi.mocked(api.image).mock.calls[0][2]!
    const next = { ...task, taskId: 'b'.repeat(32), input: { ...task.input, fileName: 'next.png' } }
    view.rerender(<VisionTaskDetail {...defaults} selectedId={next.taskId} />)
    expect(screen.queryByText('bus.jpg')).toBeNull()
    expect(signal.aborted).toBe(true)
    view.rerender(<VisionTaskDetail {...defaults} task={next} selectedId={next.taskId} result={{ ...result, taskId: next.taskId }} />)
    expect(await screen.findByAltText('任务原图')).toBeTruthy()
    await act(async () => { old.resolve(imageBlob) })
    expect(createUrl).toHaveBeenCalledTimes(1)
    expect(screen.queryByText('bus.jpg')).toBeNull()
  })

  it('releases the image when hidden or unmounted and reloads only when active', async () => {
    const view = render(<VisionTaskDetail {...defaults} />)
    await screen.findByAltText('任务原图')
    const signal = vi.mocked(api.image).mock.calls[0][2]!
    view.rerender(<VisionTaskDetail {...defaults} active={false} />)
    expect(signal.aborted).toBe(true)
    expect(revokeUrl).toHaveBeenCalledWith('blob:image-1')
    expect(screen.queryByAltText('任务原图')).toBeNull()
    expect(api.image).toHaveBeenCalledTimes(1)
    view.rerender(<VisionTaskDetail {...defaults} />)
    await screen.findByAltText('任务原图')
    expect(api.image).toHaveBeenCalledTimes(2)
    view.unmount(); expect(revokeUrl).toHaveBeenCalledWith('blob:image-2')
  })

  it('shows image error details, retries and does not leave an earlier variant visible', async () => {
    vi.mocked(api.image).mockResolvedValueOnce(imageBlob).mockRejectedValueOnce(new ApiError('图片暂不可读取', 'INPUT_UNAVAILABLE', 503, 'image-request'))
    render(<VisionTaskDetail {...defaults} />)
    await screen.findByAltText('任务原图')
    fireEvent.click(screen.getByRole('button', { name: '检测框图' }))
    await waitFor(() => expect(screen.getByRole('alert').textContent).toContain('image-request'))
    expect(screen.queryByRole('img')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: '重新读取图片' }))
    expect(await screen.findByAltText('目标检测框图')).toBeTruthy()
  })

  it('handles a browser image decode failure without retaining the broken image', async () => {
    render(<VisionTaskDetail {...defaults} />)
    fireEvent.error(await screen.findByAltText('任务原图'))
    expect(screen.getByRole('alert').textContent).toContain('无法显示')
    expect(screen.queryByRole('img')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: '重新读取图片' }))
    expect(await screen.findByAltText('任务原图')).toBeTruthy()
  })

  it('downloads exact JSON values and reuses the currently displayed annotated PNG', async () => {
    render(<VisionTaskDetail {...defaults} />)
    await screen.findByAltText('任务原图')
    fireEvent.click(screen.getByRole('button', { name: '下载 JSON' }))
    expect(format.downloadText).toHaveBeenCalledWith('bus-aaaaaaaa.json', JSON.stringify(result, null, 2), 'application/json;charset=utf-8')
    expect(vi.mocked(format.downloadText).mock.calls[0][1]).toContain('16.12345678')
    fireEvent.click(screen.getByRole('button', { name: '检测框图' }))
    await screen.findByAltText('目标检测框图')
    fireEvent.click(screen.getByRole('button', { name: '下载框图 PNG' }))
    expect(format.downloadBlob).toHaveBeenCalledWith('bus-aaaaaaaa-annotated.png', imageBlob)
    expect(api.image).toHaveBeenCalledTimes(2)
  })

  it('fetches a PNG download independently of original preview and suppresses download after leaving', async () => {
    const pending = deferred<Blob>()
    vi.mocked(api.image).mockResolvedValueOnce(imageBlob).mockReturnValueOnce(pending.promise)
    const view = render(<VisionTaskDetail {...defaults} />)
    await screen.findByAltText('任务原图')
    fireEvent.click(screen.getByRole('button', { name: '下载框图 PNG' }))
    expect((screen.getByRole('button', { name: '正在读取框图…' }) as HTMLButtonElement).disabled).toBe(true)
    const signal = vi.mocked(api.image).mock.calls[1][2]!
    view.rerender(<VisionTaskDetail {...defaults} active={false} />)
    expect(signal.aborted).toBe(true)
    await act(async () => { pending.resolve(imageBlob) })
    expect(format.downloadBlob).not.toHaveBeenCalled()
  })

  it('reports failed PNG download and permits a retry', async () => {
    vi.mocked(api.image).mockResolvedValueOnce(imageBlob).mockRejectedValueOnce(new ApiError('连接失败', 'NETWORK_ERROR')).mockResolvedValueOnce(imageBlob)
    render(<VisionTaskDetail {...defaults} />)
    await screen.findByAltText('任务原图')
    fireEvent.click(screen.getByRole('button', { name: '下载框图 PNG' }))
    await waitFor(() => expect(screen.getByRole('alert').textContent).toContain('框图下载失败'))
    fireEvent.click(screen.getByRole('button', { name: '下载框图 PNG' }))
    await waitFor(() => expect(format.downloadBlob).toHaveBeenCalledOnce())
  })

  it('treats zero detections as success and permits both result downloads', async () => {
    render(<VisionTaskDetail {...defaults} result={{ ...result, output: { ...result.output, detections: [] } }} />)
    await screen.findByAltText('任务原图')
    expect(screen.getByText('检测成功，本次没有发现目标')).toBeTruthy()
    expect(screen.getByText('检测到 0 个目标')).toBeTruthy()
    expect(screen.queryByRole('table')).toBeNull()
    expect((screen.getByRole('button', { name: '下载 JSON' }) as HTMLButtonElement).disabled).toBe(false)
    expect((screen.getByRole('button', { name: '检测框图' }) as HTMLButtonElement).disabled).toBe(false)
  })

  it('offers keyboard-operable result tabs with actual version, device and timing information', async () => {
    render(<VisionTaskDetail {...defaults} />)
    await screen.findByAltText('任务原图')
    const detectionsTab = screen.getByRole('tab', { name: '检测目标' })
    fireEvent.keyDown(detectionsTab, { key: 'ArrowRight' })
    const executionTab = screen.getByRole('tab', { name: '执行信息' })
    expect(document.activeElement).toBe(executionTab)
    expect(executionTab.getAttribute('aria-selected')).toBe('true')
    expect(screen.getByText('CPU / float16')).toBeTruthy()
    expect(screen.getByText(task.modelVersion)).toBeTruthy()
    expect(screen.getByText('ONNX Runtime 1.31.0 · pillow-bilinear-letterbox-v1')).toBeTruthy()
    fireEvent.keyDown(executionTab, { key: 'Home' })
    expect(screen.getByRole('table')).toBeTruthy()
  })

  it('allows cancelling a running detection and retains the accepted state until confirmed', async () => {
    const onCancel = vi.fn().mockResolvedValue(null)
    const view = render(<VisionTaskDetail {...defaults} task={{ ...task, status: 'running', message: '正在检测' }} result={null} onCancel={onCancel} />)
    await screen.findByAltText('任务原图')
    expect(screen.getByText('检测中', { exact: true })).toBeTruthy()
    expect((screen.getByRole('button', { name: '检测框图' }) as HTMLButtonElement).disabled).toBe(true)
    fireEvent.click(screen.getByRole('button', { name: '取消任务' })); expect(onCancel).toHaveBeenCalledOnce()
    view.rerender(<VisionTaskDetail {...defaults} task={{ ...task, status: 'cancel_requested' }} result={null} onCancel={onCancel} cancellation={{ pending: false, error: null, uncertain: false, decision: 'cancel_requested' }} />)
    expect((screen.getByRole('button', { name: '取消请求已受理' }) as HTMLButtonElement).disabled).toBe(true)
    expect(screen.getByText(/取消中不等于推理子进程已经停止/)).toBeTruthy()
    view.rerender(<VisionTaskDetail {...defaults} task={{ ...task, status: 'cancelled' }} onCancel={onCancel} />)
    expect(screen.queryByRole('button', { name: '下载 JSON' })).toBeNull()
    expect(screen.queryByRole('button', { name: '取消任务' })).toBeNull()
  })

  it('shows uncertain cancellation, bounded retry and final failure without invented progress', async () => {
    const onCancel = vi.fn().mockResolvedValue(null)
    const view = render(<VisionTaskDetail {...defaults} task={{ ...task, status: 'retry_wait', error: { code: 'CHILD_EXITED', message: '子进程退出' }, nextRetryAt: '2026-10-13T04:00:10Z' }} result={null} onCancel={onCancel} cancellation={{ pending: false, error: { message: '响应未确认', requestId: 'cancel-request' }, uncertain: true, decision: null }} />)
    await screen.findByAltText('任务原图')
    expect(screen.getByText(/最近一次错误.*CHILD_EXITED/)).toBeTruthy()
    expect(screen.getByRole('alert').textContent).toContain('cancel-request')
    fireEvent.click(screen.getByRole('button', { name: '重试取消' })); expect(onCancel).toHaveBeenCalledOnce()
    expect(screen.queryByRole('progressbar')).toBeNull()
    view.rerender(<VisionTaskDetail {...defaults} task={{ ...task, status: 'failed', error: { code: 'DETECTION_FAILED', message: '模型执行失败' } }} result={null} />)
    expect(screen.getByText('任务已结束，不会继续自动重试。')).toBeTruthy()
    expect(screen.getByText(/失败原因.*DETECTION_FAILED/)).toBeTruthy()
    expect(screen.queryByRole('button', { name: '下载 JSON' })).toBeNull()
  })

  it('does not expose another task result and keeps result reload independent of the original image', async () => {
    const onRefresh = vi.fn()
    render(<VisionTaskDetail {...defaults} result={{ ...result, taskId: 'b'.repeat(32) }} resultError={{ message: '结果暂不可用', requestId: 'result-request' }} onRefresh={onRefresh} />)
    await screen.findByAltText('任务原图')
    expect(screen.queryByRole('table')).toBeNull()
    expect(screen.queryByRole('button', { name: '下载 JSON' })).toBeNull()
    expect(screen.getByRole('alert').textContent).toContain('result-request')
    fireEvent.click(screen.getByRole('button', { name: '重新读取结果' })); expect(onRefresh).toHaveBeenCalledOnce()
  })
})

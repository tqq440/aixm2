import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { TaskDetail } from './TaskDetail'
import type { TaskResultView, TaskView } from '../types/api'
import * as format from '../utils/format'

const sample: TaskView = {
  taskId: 'a'.repeat(32), callerId: 'local-user', taskType: 'asr', modelId: 'whisper-small', modelVersion: 'rev',
  parameters: { language: 'zh' }, status: 'running', attempt: 1, maxAttempts: 2, executionPlanVersion: 1,
  input: { fileName: 'speech.wav', sizeBytes: 100, sha256: 'a'.repeat(64), durationSeconds: 5 },
  createdAt: '2026-10-12T10:00:00Z', updatedAt: '2026-10-12T10:00:01Z', startedAt: '2026-10-12T10:00:01Z', finishedAt: null,
  queueDeadlineAt: '2026-10-12T10:10:00Z', totalDeadlineAt: '2026-10-12T10:30:00Z',
  attemptDeadlineAt: '2026-10-12T10:15:01Z', nextRetryAt: null, error: null, resultAvailable: false,
  message: '后台正在执行任务。',
}

const common = { result: null, selectedId: sample.taskId, taskError: null, resultError: null, loading: false, onRefresh: vi.fn() }
afterEach(() => { cleanup(); vi.restoreAllMocks() })

describe('task cancellation and recovery details', () => {
  it('shows an unknown upstream version for a queued task before a result exists', () => {
    render(<TaskDetail {...common} task={{ ...sample, status: 'queued', modelVersion: 'integration-fixture', remoteOutcome: 'not_dispatched',
      versionInfo: { versionBasis: 'integration_revision', upstreamVersion: null } }} onCancel={vi.fn()} cancellation={null} />)
    expect(screen.getByText(/上游模型版本未知 · 接入修订/).textContent).toContain('integration-fixture')
  })

  it('allows queued external cancellation, but disables it after claim with a backend reason', () => {
    const onCancel = vi.fn()
    const view = render(<TaskDetail {...common} task={{ ...sample, status: 'queued', remoteOutcome: 'not_dispatched', cancelAllowed: true }} onCancel={onCancel} cancellation={null} />)
    fireEvent.click(screen.getByRole('button', { name: '取消任务' }))
    expect(onCancel).toHaveBeenCalledOnce()
    view.rerender(<TaskDetail {...common} task={{ ...sample, remoteOutcome: 'pending', cancelAllowed: false, cancelUnavailableReason: '调用开始后不支持取消' }} onCancel={onCancel} cancellation={null} />)
    fireEvent.click(screen.getByRole('button', { name: '取消任务' }))
    expect(onCancel).toHaveBeenCalledOnce()
    expect((screen.getByRole('button', { name: '取消任务' }) as HTMLButtonElement).disabled).toBe(true)
    expect(screen.getByText('调用开始后不支持取消')).toBeTruthy()
    expect(screen.queryByText(/执行中需要等待后台停止/)).toBeNull()
  })

  it('does not equate a failed local task with the remote request having stopped', () => {
    render(<TaskDetail {...common} task={{ ...sample, status: 'failed', remoteOutcome: 'unknown', cancelAllowed: false, maxAttempts: 1 }} onCancel={vi.fn()} cancellation={null} />)
    expect(screen.getByText('远端结果未知')).toBeTruthy()
    expect(screen.getByText(/断开连接或本机任务结束不能证明远端推理已停止/)).toBeTruthy()
    expect(screen.getByText(/是否仍占用许可、其他新任务能否受理，以调用记录、调用通道与共享容量状态为准/)).toBeTruthy()
    expect(screen.queryByText(/原请求继续占用许可/)).toBeNull()
    expect(screen.queryByRole('button', { name: /取消/ })).toBeNull()
  })
  it('keeps cancellation pending until the backend returns a final status', () => {
    const onCancel = vi.fn().mockResolvedValue(null)
    const view = render(<TaskDetail {...common} task={sample} onCancel={onCancel} cancellation={null} />)
    fireEvent.click(screen.getByRole('button', { name: '取消任务' }))
    expect(onCancel).toHaveBeenCalledOnce()
    view.rerender(<TaskDetail {...common} task={{ ...sample, status: 'cancel_requested', message: '正在停止推理子进程。' }} onCancel={onCancel} cancellation={{ pending: false, error: null, uncertain: false, decision: 'cancel_requested' }} />)
    expect((screen.getByRole('button', { name: '取消请求已受理' }) as HTMLButtonElement).disabled).toBe(true)
    expect(screen.queryByText('已取消', { exact: true })).toBeNull()
    expect(screen.getByText(/取消中不等于推理子进程已经停止/)).toBeTruthy()
    view.rerender(<TaskDetail {...common} task={{ ...sample, status: 'cancelled', message: '任务已经取消。' }} onCancel={onCancel} cancellation={null} />)
    expect(screen.getByText('已取消', { exact: true })).toBeTruthy()
    expect(screen.queryByRole('button', { name: /取消/ })).toBeNull()
  })

  it('allows a lost-response retry and replaces uncertainty once a later read confirms the request', () => {
    const cancellation = { pending: false, error: { message: '连接中断', requestId: 'request-lost' }, uncertain: true, decision: null }
    const onCancel = vi.fn().mockResolvedValue(null)
    const view = render(<TaskDetail {...common} task={sample} onCancel={onCancel} cancellation={cancellation} />)
    expect(screen.getByRole('alert').textContent).toContain('request-lost')
    expect(screen.getByRole('status').textContent).toContain('后台可能已收到取消请求')
    fireEvent.click(screen.getByRole('button', { name: '重试取消' }))
    expect(onCancel).toHaveBeenCalledOnce()
    view.rerender(<TaskDetail {...common} task={{ ...sample, status: 'cancel_requested' }} onCancel={onCancel} cancellation={cancellation} />)
    expect(screen.queryByRole('alert')).toBeNull()
    expect(screen.queryByText(/后台可能已收到取消请求/)).toBeNull()
  })

  it('shows the retry reason and fixed deadlines without claiming a completion percentage', () => {
    render(<TaskDetail {...common} task={{ ...sample, status: 'retry_wait', nextRetryAt: '2026-10-12T10:00:10Z', error: { code: 'WORKER_STOPPED', message: 'Worker 已退出。' } }} onCancel={vi.fn()} cancellation={null} />)
    expect(screen.getByText('等待重试', { exact: true })).toBeTruthy()
    expect(screen.getByText(/最近一次错误.*Worker 已退出.*WORKER_STOPPED/)).toBeTruthy()
    expect(screen.getByText('执行尝试 1 / 2 次')).toBeTruthy()
    const summary = screen.getByText('执行尝试 1 / 2 次').closest('summary')!
    fireEvent.click(summary)
    expect(screen.getByText('排队截止时间')).toBeTruthy()
    expect(screen.getByText('任务总截止时间')).toBeTruthy()
    expect(screen.getByText('本次执行截止时间')).toBeTruthy()
    expect(screen.getByText('下次重试时间')).toBeTruthy()
    expect(screen.getByText(/最大次数包含首次执行/)).toBeTruthy()
    expect(screen.queryByRole('progressbar')).toBeNull()
  })
})

/** Contract fixtures only: none of these extra engines are installed or executed. */
describe('ASR result metadata compatibility', () => {
  const task: TaskView = { ...sample, status: 'succeeded', resultAvailable: true }
  const base: TaskResultView = {
    taskId: sample.taskId, modelId: 'asr-contract-fixture', modelVersion: 'fixture-revision', schemaVersion: '1',
    output: { text: '保留原始文本。', segments: [{ start: 0, end: 1.2, text: '保留原始文本。' }],
      language: 'zh', languageRequested: 'zh', languageProbability: null, audioDurationSeconds: 5 },
  }
  const show = (result: TaskResultView) => {
    render(<TaskDetail {...common} task={task} result={result} onCancel={vi.fn()} cancellation={null} />)
    fireEvent.click(screen.getByRole('tab', { name: '执行信息' }))
  }
  const value = (label: string) => screen.getByText(label, { selector: 'dt' }).nextElementSibling?.textContent

  it('keeps unknown upstream metadata explicit in execution details and downloaded JSON', () => {
    const download = vi.spyOn(format, 'downloadText').mockImplementation(() => {})
    const result: TaskResultView = { ...base, modelVersion: 'integration-fixture',
      versionInfo: { versionBasis: 'integration_revision', upstreamVersion: null } }
    show(result)
    expect(value('上游模型版本')).toBe('未知')
    expect(value('接入修订')).toBe('integration-fixture')
    expect(screen.queryByText('模型版本', { selector: 'dt' })).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: '下载 JSON' }))
    const body = JSON.parse(download.mock.calls.at(-1)![1]) as TaskResultView
    expect(body.versionInfo).toEqual({ versionBasis: 'integration_revision', upstreamVersion: null })
    expect(body.modelVersion).toBe(result.modelVersion)
  })

  it('reads historical flat Whisper metadata without requiring a new metadata object', () => {
    show({ ...base, output: { ...base.output, device: 'cpu', computeType: 'int8_float32', transcriptionSeconds: 1.25,
      totalSeconds: 1.5, fasterWhisperVersion: '1.2.3', ctranslate2Version: '4.5.6' } })
    expect(value('执行设备 / 计算类型')).toBe('CPU / int8_float32')
    expect(value('推理耗时')).toBe('1.25 秒')
    expect(value('运行库')).toBe('faster-whisper 1.2.3 · CTranslate2 4.5.6')
    expect(screen.queryByText('推理适配器')).toBeNull()
    expect(value('模型版本')).toBe(base.modelVersion)
    expect(screen.queryByText('接入修订', { selector: 'dt' })).toBeNull()
  })

  it('renders a generic result without falsely supplying a device, language, library or timing', () => {
    show({ ...base, output: { ...base.output, language: null } })
    expect(value('识别语言')).toBe('未报告')
    expect(value('执行设备 / 计算类型')).toBe('未报告 / 未报告')
    expect(value('推理耗时')).toBe('未报告')
    expect(value('本次处理耗时')).toBe('未报告')
    expect(value('运行库')).toBe('未报告')
    expect(screen.getByRole('tabpanel').textContent).not.toMatch(/undefined|NaN|faster-whisper|CTranslate2|CPU|int8/)
    fireEvent.click(screen.getByRole('tab', { name: '转写全文' }))
    expect(screen.getByTestId('transcript-text').textContent).toBe(base.output.text)
    fireEvent.click(screen.getByRole('tab', { name: '时间分段' }))
    expect(screen.getByRole('tabpanel').textContent).toContain('00:01.20')
    expect(screen.getByRole('tabpanel').textContent).toContain(base.output.text)
  })

  it('uses reported engine metadata including zero timings without creating Whisper runtime labels', () => {
    show({ ...base, output: { ...base.output, engineMetadata: { adapterKey: 'test-only-asr', device: 'test-device',
      computeType: null, transcriptionSeconds: 0, totalSeconds: 0.2, runtimeVersions: { 'test-runtime': 'fixture-2' } } } })
    expect(value('推理适配器')).toBe('test-only-asr')
    expect(value('执行设备 / 计算类型')).toBe('TEST-DEVICE / 未报告')
    expect(value('推理耗时')).toBe('0.00 秒')
    expect(value('运行库')).toBe('test-runtime fixture-2')
    expect(screen.getByRole('tabpanel').textContent).not.toMatch(/faster-whisper|CTranslate2/)
  })

  it('falls back to existing legacy measurements when nullable metadata omits them', () => {
    show({ ...base, output: { ...base.output, device: 'cpu', computeType: 'int8', transcriptionSeconds: 0.4,
      fasterWhisperVersion: 'legacy-version', engineMetadata: { adapterKey: 'faster-whisper', device: null,
        transcriptionSeconds: null, runtimeVersions: {} } } })
    expect(value('执行设备 / 计算类型')).toBe('CPU / int8')
    expect(value('推理耗时')).toBe('0.40 秒')
    expect(value('运行库')).toBe('faster-whisper legacy-version')
  })

  it('downloads the exact received JSON and text without adding display fallbacks or dropping metadata', () => {
    const download = vi.spyOn(format, 'downloadText').mockImplementation(() => {})
    const result: TaskResultView = { ...base, output: { ...base.output, language: null,
      engineMetadata: { adapterKey: 'test-only-asr', runtimeVersions: { 'test-runtime': 'fixture-2' } } } }
    const original = structuredClone(result)
    show(result)
    fireEvent.click(screen.getByRole('button', { name: '下载 JSON' }))
    expect(download).toHaveBeenLastCalledWith('speech-aaaaaaaa.json', JSON.stringify(original, null, 2), 'application/json;charset=utf-8')
    fireEvent.click(screen.getByRole('button', { name: '下载 TXT' }))
    expect(download).toHaveBeenLastCalledWith('speech-aaaaaaaa.txt', original.output.text, 'text/plain;charset=utf-8')
    expect(result).toEqual(original)
  })
})

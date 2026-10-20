import { useEffect, useState } from 'react'
import type { KeyboardEvent } from 'react'
import type { CancellationReceipt, TaskResultView, TaskView } from '../types/api'
import { bytes, dateTime, downloadText, languageName, resultFileName, seconds, timestamp } from '../utils/format'
import { hasUnknownUpstreamVersion } from '../utils/modelVersion'
import { ErrorNotice, StatusBadge } from './Feedback'
import type { ErrorInfo } from './Feedback'
import { Icon } from './Icon'
import { TaskRoutingPanel } from '@/components/TaskRoutingPanel'

interface Props {
  task: TaskView | null; result: TaskResultView | null; selectedId: string | null
  taskError: ErrorInfo | null; resultError: ErrorInfo | null; loading: boolean; onRefresh: () => void
  onCancel: () => Promise<unknown>
  cancellation: { pending: boolean; error: ErrorInfo | null; uncertain: boolean; decision: CancellationReceipt['decision'] | null } | null
}
const tabs = ['转写全文', '时间分段', '执行信息'] as const
const executionTitles = {
  created: '任务已保存，等待后台处理', queued: '任务已排队，等待执行', running: '正在将音频转换为文字',
  retry_wait: '暂时故障，等待有限重试', cancel_requested: '取消请求已受理，正在等待后台确认',
  cancelled: '任务已取消', failed: '这次转写未能完成',
} as const

export function TaskDetail({ task, result, selectedId, taskError, resultError, loading, onRefresh, onCancel, cancellation }: Props) {
  const [tab, setTab] = useState(0)
  const [feedback, setFeedback] = useState('')
  const terminal = task && ['succeeded', 'failed', 'cancelled'].includes(task.status)
  const external = task?.remoteOutcome != null
  const cancelling = cancellation?.pending ?? false
  const metadata = result?.output.engineMetadata
  const device = metadata?.device ?? result?.output.device
  const computeType = metadata?.computeType ?? result?.output.computeType
  const runtimeVersions = {
    ...(result?.output.fasterWhisperVersion ? { 'faster-whisper': result.output.fasterWhisperVersion } : {}),
    ...(result?.output.ctranslate2Version ? { ctranslate2: result.output.ctranslate2Version } : {}),
    ...metadata?.runtimeVersions,
  }
  const runtimeDescription = Object.entries(runtimeVersions)
    .map(([name, version]) => `${name === 'ctranslate2' ? 'CTranslate2' : name} ${version}`).join(' · ') || '未报告'
  useEffect(() => { setTab(0); setFeedback('') }, [selectedId])
  const copy = async () => {
    if (!result) return
    try { await navigator.clipboard.writeText(result.output.text); setFeedback('文字已复制') }
    catch { setFeedback('浏览器未允许复制，请选中文字后手动复制。') }
  }
  const download = (format: 'txt' | 'json') => {
    if (!task || !result) return
    const name = resultFileName(task.input.fileName, task.taskId)
    downloadText(`${name}.${format}`, format === 'txt' ? result.output.text : JSON.stringify(result, null, 2), format === 'txt' ? 'text/plain;charset=utf-8' : 'application/json;charset=utf-8')
    setFeedback(`已发起 ${format.toUpperCase()} 下载`)
  }
  const keyboardTabs = (event: KeyboardEvent<HTMLButtonElement>) => {
    let next = tab
    if (event.key === 'ArrowRight') next = (tab + 1) % tabs.length
    else if (event.key === 'ArrowLeft') next = (tab + tabs.length - 1) % tabs.length
    else if (event.key === 'Home') next = 0
    else if (event.key === 'End') next = tabs.length - 1
    else return
    event.preventDefault(); setTab(next); document.getElementById(`detail-tab-${next}`)?.focus()
  }
  return <section className="panel detail-panel" aria-label="任务详情" id="task-detail" aria-busy={loading}>
    <div className="panel-heading"><div><span className="section-kicker">TRANSCRIPTION RESULT</span><h2>任务详情</h2></div>{task && <StatusBadge status={task.status} />}</div>
    <ErrorNotice error={taskError} title="任务详情读取失败" />
    {!selectedId && <div className="empty-state detail-empty"><span className="empty-icon"><Icon name="wave" size={32} /></span><h3>让每一段声音，都有文字记录</h3><p>选择一个任务查看详情，或上传音频开始转写。</p></div>}
    {selectedId && !task && !taskError && <div className="loading-state"><Icon name="refresh" className="spin" /><span>正在读取任务详情…</span></div>}
    {task && <>
      <div className="detail-file-heading"><h3 title={task.input.fileName}>{task.input.fileName}</h3><p><span>{seconds(task.input.durationSeconds)}</span><span>{bytes(task.input.sizeBytes)}</span><span>{external ? '语言由服务端决定' : languageName(task.parameters.language)}</span></p></div>
      <div className="task-reference"><span>任务编号</span><code>{task.taskId}</code></div>
      <TaskRoutingPanel task={task} untrusted={Boolean(taskError)} />
      {hasUnknownUpstreamVersion(task) && <p className="field-hint model-version">上游模型版本未知 · 接入修订：<code>{task.modelVersion}</code></p>}
      {task.remoteOutcome === 'unknown' && <div className="execution-message execution-failed" role="status"><Icon name="warning" size={26} /><div><h3>远端结果未知</h3><p>断开连接或本机任务结束不能证明远端推理已停止。本次调用不会自动重发，须由管理员核对；是否仍占用许可、其他新任务能否受理，以调用记录、调用通道与共享容量状态为准。</p></div></div>}
      <details className="task-limits">
        <summary><strong>执行尝试 {task.attempt} / {task.maxAttempts} 次</strong><span>查看期限</span></summary>
        <dl className="execution-grid">
          <div><dt>排队截止时间</dt><dd>{dateTime(task.queueDeadlineAt)}</dd></div><div><dt>任务总截止时间</dt><dd>{dateTime(task.totalDeadlineAt)}</dd></div>
          <div><dt>本次执行截止时间</dt><dd>{dateTime(task.attemptDeadlineAt)}</dd></div><div><dt>下次重试时间</dt><dd>{dateTime(task.nextRetryAt)}</dd></div>
        </dl>
        <p>{external ? '外部调用最多尝试一次，不自动重发。等待资源不增加次数，原排队和任务总期限保持。' : '最大次数包含首次执行；等待资源不增加次数。重试不会延长排队和任务总期限，未记录的时间显示为 —。'}</p>
      </details>
      {task.status !== 'succeeded' && <div className={`execution-message ${task.status === 'failed' ? 'execution-failed' : ''}`}>
        <Icon name={task.status === 'failed' ? 'warning' : task.status === 'running' ? 'wave' : task.status === 'cancelled' ? 'close' : 'clock'} size={26} />
        <div><h3>{executionTitles[task.status]}</h3><p>{task.message}</p>
          {task.error && <p>{task.status === 'failed' ? '失败原因' : '最近一次错误'}：{task.error.message}（{task.error.code}）</p>}
          {task.status === 'retry_wait' && task.nextRetryAt && <p>下次重试时间：{dateTime(task.nextRetryAt)}</p>}
          <small>{task.status === 'cancel_requested' ? '取消中不等于推理子进程已经停止。后台确认后会更新任务；Worker 是否可接单请查看运行状态。'
            : task.status === 'failed' ? '任务已结束，不会继续自动重试。'
              : task.status === 'cancelled' ? '后台已将任务标记为取消，不再接受结果或安排重试；Worker 是否可接单以运行状态为准。'
                : '状态会自动更新，无需重复提交。'}</small></div>
      </div>}
      {!terminal && <div className="cancel-task-area">
        <div className="cancel-task-action"><button type="button" className="button button-cancel" onClick={() => void onCancel()} disabled={task.cancelAllowed === false || cancelling || task.status === 'cancel_requested' || Boolean(cancellation?.decision)}><Icon name={cancelling ? 'refresh' : 'close'} className={cancelling ? 'spin' : undefined} size={16} />{cancelling ? '正在请求取消…' : task.status === 'cancel_requested' ? '取消请求已受理' : cancellation?.error ? '重试取消' : '取消任务'}</button><span>{task.cancelUnavailableReason ?? (external ? '仅排队且尚未领取时可取消；开始调用后不支持取消。' : '未执行时直接取消；执行中需要等待后台停止。')}</span></div>
        <ErrorNotice error={task.status === 'cancel_requested' ? null : cancellation?.error ?? null} title={cancellation?.error?.code === 'REMOTE_ASR_CANCEL_UNSUPPORTED' ? '取消请求未受理' : '取消请求未确认'} />
        {cancellation?.uncertain && task.status !== 'cancel_requested' && <p className="cancel-feedback" role="status">响应未确认，后台可能已收到取消请求。正在重新查询状态；仍未结束时可以安全重试取消。</p>}
        {cancellation?.decision && task.status !== 'cancel_requested' && <p className="cancel-feedback" role="status">{cancellation.decision === 'already_finished' ? '任务已经结束，正在读取后台最终状态。' : '取消请求已受理，正在读取后台状态。'}</p>}
      </div>}
      {terminal && cancellation?.decision === 'already_finished' && <p className="cancel-feedback" role="status">发起取消时任务已结束，已保留后台确定的最终状态。</p>}
      {task.status === 'succeeded' && !result && !resultError && <div className="loading-state"><Icon name="refresh" className="spin" /><span>转写完成，正在读取结果…</span></div>}
      {resultError && <div className="result-error"><ErrorNotice error={resultError} title="结果读取失败" /><button type="button" className="text-button" onClick={onRefresh}>重新读取结果</button></div>}
      {result && <>
        <div className="result-toolbar"><div className="detail-tabs" role="tablist" aria-label="结果视图">{tabs.map((label, index) => <button type="button" role="tab" id={`detail-tab-${index}`} aria-controls={`detail-panel-${index}`} aria-selected={tab === index} tabIndex={tab === index ? 0 : -1} key={label} className={tab === index ? 'is-active' : ''} onClick={() => setTab(index)} onKeyDown={keyboardTabs}>{label}</button>)}</div></div>
        <div className="result-content" role="tabpanel" id={`detail-panel-${tab}`} aria-labelledby={`detail-tab-${tab}`} tabIndex={0}>
          {tab === 0 && <><div className="transcript-text" data-testid="transcript-text">{result.output.text || '本次转写没有识别到文字。'}</div><p className="result-disclaimer">模型原始转写 · 专有名词及关键内容请人工校对</p></>}
          {tab === 1 && <div className="segment-list">{result.output.segments.length ? result.output.segments.map((segment, index) => <div className="segment" key={`${index}-${segment.start}`}><span className="segment-index">{String(index + 1).padStart(2, '0')}</span><span className="segment-time">{timestamp(segment.start)}<span>—</span>{timestamp(segment.end)}</span><p>{segment.text}</p></div>) : <p className="muted">模型没有返回时间分段。</p>}</div>}
          {tab === 2 && <dl className="execution-grid">
            <div><dt>实际模型</dt><dd>{result.modelId}</dd></div><div><dt>识别语言</dt><dd>{result.output.language ? languageName(result.output.language) : '未报告'}</dd></div>
            <div><dt>执行设备 / 计算类型</dt><dd>{device?.toUpperCase() ?? '未报告'} / {computeType ?? '未报告'}</dd></div><div><dt>执行尝试次数 / 最大次数</dt><dd>{task.attempt} / {task.maxAttempts}</dd></div>
            <div><dt>推理耗时</dt><dd>{seconds(metadata?.transcriptionSeconds ?? result.output.transcriptionSeconds)}</dd></div><div><dt>本次处理耗时</dt><dd>{seconds(metadata?.totalSeconds ?? result.output.totalSeconds)}</dd></div>
            <div><dt>开始时间</dt><dd>{dateTime(task.startedAt)}</dd></div><div><dt>完成时间</dt><dd>{dateTime(task.finishedAt)}</dd></div>
            {hasUnknownUpstreamVersion(result) && <div className="wide"><dt>上游模型版本</dt><dd>未知</dd></div>}
            <div className="wide"><dt>{hasUnknownUpstreamVersion(result) ? '接入修订' : '模型版本'}</dt><dd className="monospace">{result.modelVersion}</dd></div>{metadata && <div className="wide"><dt>推理适配器</dt><dd>{metadata.adapterKey}</dd></div>}<div className="wide"><dt>运行库</dt><dd>{runtimeDescription}</dd></div>
          </dl>}
        </div>
        <div className="result-actions"><button className="button button-quiet" type="button" onClick={() => void copy()}><Icon name="copy" size={16} />复制文字</button><div className="download-actions"><button className="button button-quiet" type="button" onClick={() => download('txt')}><Icon name="download" size={16} />下载 TXT</button><button className="button button-quiet" type="button" onClick={() => download('json')}><Icon name="download" size={16} />下载 JSON</button></div></div>
        <div className="action-feedback" role="status">{feedback}</div>
      </>}
      <div className="detail-footnote"><Icon name="clock" size={13} /><span>提交于 {dateTime(task.createdAt)} · 时间按本机时区显示</span></div>
    </>}
  </section>
}

import { useEffect, useRef, useState } from 'react'
import type { KeyboardEvent } from 'react'
import { api, asApiError } from '../api/client'
import type { CancellationReceipt, DetectionResult, TaskResultView, VisionTaskView } from '../types/api'
import { bytes, dateTime, downloadBlob, downloadText, resultFileName, seconds } from '../utils/format'
import { ErrorNotice, StatusBadge } from './Feedback'
import type { ErrorInfo } from './Feedback'
import { Icon } from './Icon'

interface Props {
  task: VisionTaskView | null; result: TaskResultView<DetectionResult> | null; selectedId: string | null
  taskError: ErrorInfo | null; resultError: ErrorInfo | null; loading: boolean; onRefresh: () => void
  onCancel: () => Promise<unknown>; active?: boolean
  cancellation: { pending: boolean; error: ErrorInfo | null; uncertain: boolean; decision: CancellationReceipt['decision'] | null } | null
}
type Variant = 'original' | 'annotated'
type ImageState = { key: string; url: string | null; blob: Blob | null; error: ErrorInfo | null }
const tabs = ['检测目标', '执行信息'] as const
const executionTitles = {
  created: '任务已保存，等待后台处理', queued: '任务已排队，等待执行', running: '正在检测图片中的目标',
  retry_wait: '暂时故障，等待有限重试', cancel_requested: '取消请求已受理，正在等待后台确认',
  cancelled: '任务已取消', failed: '这次检测未能完成',
} as const

export function VisionTaskDetail({ task: receivedTask, result: receivedResult, selectedId, taskError, resultError, loading, onRefresh, onCancel, cancellation, active = true }: Props) {
  const task = receivedTask?.taskId === selectedId ? receivedTask : null
  const result = task?.status === 'succeeded' && receivedResult?.taskId === task.taskId ? receivedResult : null
  const [tab, setTab] = useState(0)
  const [view, setView] = useState<{ taskId: string | null; variant: Variant }>({ taskId: null, variant: 'original' })
  const [image, setImage] = useState<ImageState | null>(null)
  const [imageRetry, setImageRetry] = useState(0)
  const [feedback, setFeedback] = useState('')
  const [downloadError, setDownloadError] = useState<ErrorInfo | null>(null)
  const [downloading, setDownloading] = useState(false)
  const downloadRequest = useRef<AbortController | null>(null)
  const variant: Variant = view.taskId === task?.taskId && (view.variant !== 'annotated' || result) ? view.variant : 'original'
  const taskId = task?.taskId ?? null
  const imageKey = `${taskId}:${variant}:${imageRetry}`
  const visibleImage = active && image?.key === imageKey ? image : null
  const terminal = task && ['succeeded', 'failed', 'cancelled'].includes(task.status)
  const cancelling = cancellation?.pending ?? false

  useEffect(() => {
    setTab(0); setFeedback(''); setDownloadError(null); setDownloading(false)
    return () => { downloadRequest.current?.abort(); downloadRequest.current = null }
  }, [selectedId, active])

  useEffect(() => {
    if (!active || !taskId) return
    const controller = new AbortController()
    let url: string | null = null
    setImage(null)
    void api.image(taskId, variant, controller.signal).then(blob => {
      if (controller.signal.aborted) return
      url = URL.createObjectURL(blob)
      setImage({ key: imageKey, url, blob, error: null })
    }).catch(error => {
      if (!controller.signal.aborted) setImage({ key: imageKey, url: null, blob: null, error: asApiError(error) })
    })
    return () => { controller.abort(); if (url) URL.revokeObjectURL(url) }
  }, [active, taskId, variant, imageKey])

  const downloadJson = () => {
    if (!task || !result) return
    downloadText(`${resultFileName(task.input.fileName, task.taskId)}.json`, JSON.stringify(result, null, 2), 'application/json;charset=utf-8')
    setFeedback('已发起 JSON 下载'); setDownloadError(null)
  }
  const downloadAnnotated = async () => {
    if (!task || !result || !active || downloading) return
    const controller = new AbortController()
    downloadRequest.current = controller
    setDownloading(true); setDownloadError(null); setFeedback('')
    try {
      const blob = variant === 'annotated' && visibleImage?.blob && !visibleImage.error
        ? visibleImage.blob : await api.image(task.taskId, 'annotated', controller.signal)
      if (controller.signal.aborted) return
      downloadBlob(`${resultFileName(task.input.fileName, task.taskId)}-annotated.png`, blob)
      setFeedback('已发起框图 PNG 下载')
    } catch (error) {
      if (!controller.signal.aborted) setDownloadError(asApiError(error))
    } finally {
      if (!controller.signal.aborted) setDownloading(false)
      if (downloadRequest.current === controller) downloadRequest.current = null
    }
  }
  const keyboardTabs = (event: KeyboardEvent<HTMLButtonElement>) => {
    let next = tab
    if (event.key === 'ArrowRight') next = (tab + 1) % tabs.length
    else if (event.key === 'ArrowLeft') next = (tab + tabs.length - 1) % tabs.length
    else if (event.key === 'Home') next = 0
    else if (event.key === 'End') next = tabs.length - 1
    else return
    event.preventDefault(); setTab(next); document.getElementById(`vision-detail-tab-${next}`)?.focus()
  }

  return <section className="panel detail-panel vision-detail-panel" aria-label="图片任务详情" id="vision-task-detail" aria-busy={loading}>
    <div className="panel-heading"><div><span className="section-kicker">IMAGE DETECTION RESULT</span><h2>图片任务详情</h2></div>{task && <StatusBadge status={task.status} taskType="vision" />}</div>
    <ErrorNotice error={taskError} title="任务详情读取失败" />
    {!selectedId && <div className="empty-state detail-empty"><span className="empty-icon"><Icon name="image" size={32} /></span><h3>发现图片中的目标</h3><p>选择一个图片任务查看详情，或上传图片开始检测。</p></div>}
    {selectedId && !task && !taskError && <div className="loading-state"><Icon name="refresh" className="spin" /><span>正在读取图片任务详情…</span></div>}
    {task && <>
      <div className="detail-file-heading"><h3 title={task.input.fileName}>{task.input.fileName}</h3><p><span>{task.input.width} × {task.input.height} 像素</span><span>{bytes(task.input.sizeBytes)}</span><span>静态图片</span></p></div>
      <div className="task-reference"><span>任务编号</span><code>{task.taskId}</code></div>
      <details className="task-limits"><summary><strong>执行尝试 {task.attempt} / {task.maxAttempts} 次</strong><span>查看期限</span></summary><dl className="execution-grid">
        <div><dt>排队截止时间</dt><dd>{dateTime(task.queueDeadlineAt)}</dd></div><div><dt>任务总截止时间</dt><dd>{dateTime(task.totalDeadlineAt)}</dd></div>
        <div><dt>本次执行截止时间</dt><dd>{dateTime(task.attemptDeadlineAt)}</dd></div><div><dt>下次重试时间</dt><dd>{dateTime(task.nextRetryAt)}</dd></div>
      </dl><p>最大次数包含首次执行；等待资源不增加次数。重试不会延长排队和任务总期限，未记录的时间显示为 —。</p></details>
      {task.status !== 'succeeded' && <div className={`execution-message ${task.status === 'failed' ? 'execution-failed' : ''}`}><Icon name={task.status === 'failed' ? 'warning' : task.status === 'cancelled' ? 'close' : 'clock'} size={26} /><div><h3>{executionTitles[task.status]}</h3><p>{task.message}</p>
        {task.error && <p>{task.status === 'failed' ? '失败原因' : '最近一次错误'}：{task.error.message}（{task.error.code}）</p>}
        {task.status === 'retry_wait' && task.nextRetryAt && <p>下次重试时间：{dateTime(task.nextRetryAt)}</p>}
        <small>{task.status === 'cancel_requested' ? '取消中不等于推理子进程已经停止。后台确认后会更新任务；Worker 是否可接单请查看运行状态。'
          : task.status === 'failed' ? '任务已结束，不会继续自动重试。' : task.status === 'cancelled' ? '后台已将任务标记为取消，不再接受结果或安排重试；Worker 是否可接单以运行状态为准。' : '状态会自动更新，无需重复提交。'}</small>
      </div></div>}
      {!terminal && <div className="cancel-task-area"><div className="cancel-task-action"><button type="button" className="button button-cancel" onClick={() => void onCancel()} disabled={cancelling || task.status === 'cancel_requested' || Boolean(cancellation?.decision)}><Icon name={cancelling ? 'refresh' : 'close'} className={cancelling ? 'spin' : undefined} size={16} />{cancelling ? '正在请求取消…' : task.status === 'cancel_requested' ? '取消请求已受理' : cancellation?.error ? '重试取消' : '取消任务'}</button><span>未执行时直接取消；执行中需要等待后台停止。</span></div>
        <ErrorNotice error={task.status === 'cancel_requested' ? null : cancellation?.error ?? null} title="取消请求未确认" />
        {cancellation?.uncertain && task.status !== 'cancel_requested' && <p className="cancel-feedback" role="status">响应未确认，后台可能已收到取消请求。正在重新查询状态；仍未结束时可以安全重试取消。</p>}
        {cancellation?.decision && task.status !== 'cancel_requested' && <p className="cancel-feedback" role="status">{cancellation.decision === 'already_finished' ? '任务已经结束，正在读取后台最终状态。' : '取消请求已受理，正在读取后台状态。'}</p>}
      </div>}
      {terminal && cancellation?.decision === 'already_finished' && <p className="cancel-feedback" role="status">发起取消时任务已结束，已保留后台确定的最终状态。</p>}
      <div className="vision-image-section">
        <div className="vision-image-toolbar"><h3>任务图片</h3><div className="vision-image-switch" role="group" aria-label="图片显示方式"><button type="button" aria-pressed={variant === 'original'} onClick={() => setView({ taskId: task.taskId, variant: 'original' })}>原图</button><button type="button" aria-pressed={variant === 'annotated'} disabled={!result} onClick={() => setView({ taskId: task.taskId, variant: 'annotated' })}>检测框图</button></div></div>
        <div className="vision-image-stage" aria-busy={active && !visibleImage}>
          {!active ? <p className="muted">切回图片工作区后加载图片。</p> : visibleImage?.error ? <div className="vision-image-error"><ErrorNotice error={visibleImage.error} title="图片读取失败" /><button type="button" className="text-button" onClick={() => setImageRetry(value => value + 1)}>重新读取图片</button></div>
            : visibleImage?.url ? <figure><img className="vision-result-image" src={visibleImage.url} alt={variant === 'annotated' ? '目标检测框图' : '任务原图'} onError={() => setImage({ key: imageKey, url: null, blob: null, error: { message: '浏览器无法显示返回的图片，请重新读取。' } })} /></figure>
              : <div className="loading-state"><Icon name="refresh" className="spin" /><span>正在读取{variant === 'annotated' ? '检测框图' : '原图'}…</span></div>}
        </div>
        <p className="vision-image-note">原图已按拍摄方向归正，透明区域合成为白色。框图由后台已保存的检测结果生成；页面显示缩放不改变原图像素坐标。</p>
      </div>
      {task.status === 'succeeded' && !result && !resultError && <div className="loading-state"><Icon name="refresh" className="spin" /><span>检测完成，正在读取结果…</span></div>}
      {resultError && <div className="result-error"><ErrorNotice error={resultError} title="结果读取失败" /><button type="button" className="text-button" onClick={onRefresh}>重新读取结果</button></div>}
      {result && <>
        <div className="result-toolbar"><div className="detail-tabs" role="tablist" aria-label="检测结果视图">{tabs.map((label, index) => <button type="button" role="tab" id={`vision-detail-tab-${index}`} aria-controls={`vision-detail-panel-${index}`} aria-selected={tab === index} tabIndex={tab === index ? 0 : -1} key={label} className={tab === index ? 'is-active' : ''} onClick={() => setTab(index)} onKeyDown={keyboardTabs}>{label}</button>)}</div></div>
        <div className="result-content" role="tabpanel" id={`vision-detail-panel-${tab}`} aria-labelledby={`vision-detail-tab-${tab}`} tabIndex={0}>
          {tab === 0 && <><div className="vision-detection-summary"><strong>检测到 {result.output.detections.length} 个目标</strong><span>原图 {result.output.imageWidth} × {result.output.imageHeight} 像素</span></div>
            {result.output.detections.length ? <div className="vision-table-scroll" tabIndex={0} role="region" aria-label="检测目标表，可横向滚动"><table className="vision-detections"><caption className="sr-only">检测目标的类别、置信度和原图像素坐标</caption><thead><tr><th scope="col">序号</th><th scope="col">类别 / ID</th><th scope="col">置信度</th><th scope="col">xMin</th><th scope="col">yMin</th><th scope="col">xMax</th><th scope="col">yMax</th></tr></thead><tbody>{result.output.detections.map((item, index) => <tr key={index}><td>{index + 1}</td><td>{item.className} / {item.classId}</td><td>{(item.confidence * 100).toFixed(2)}%</td><td>{item.box.xMin.toFixed(2)}</td><td>{item.box.yMin.toFixed(2)}</td><td>{item.box.xMax.toFixed(2)}</td><td>{item.box.yMax.toFixed(2)}</td></tr>)}</tbody></table></div>
              : <div className="vision-empty-detections"><h3>检测成功，本次没有发现目标</h3><p>当前模型与阈值下没有保留的检测框。空检测是有效结果，仍可下载 JSON 和图片。</p></div>}
            <p className="result-disclaimer">坐标原点为归正后原图左上角，x 向右、y 向下；表格显示保留两位小数，JSON 保留原始数值。模型结果请结合实际图片核对。</p>
          </>}
          {tab === 1 && <dl className="execution-grid"><div><dt>实际模型</dt><dd>{result.modelId}</dd></div><div><dt>执行设备 / 输入类型</dt><dd>{result.output.device.toUpperCase()} / {result.output.inputType}</dd></div>
            <div><dt>执行尝试次数 / 最大次数</dt><dd>{task.attempt} / {task.maxAttempts}</dd></div><div><dt>模型加载耗时</dt><dd>{seconds(result.output.loadSeconds)}</dd></div>
            <div><dt>推理耗时</dt><dd>{seconds(result.output.inferenceSeconds)}</dd></div><div><dt>本次处理耗时</dt><dd>{seconds(result.output.totalSeconds)}</dd></div>
            <div><dt>开始时间</dt><dd>{dateTime(task.startedAt)}</dd></div><div><dt>完成时间</dt><dd>{dateTime(task.finishedAt)}</dd></div>
            <div><dt>置信度 / 重叠阈值</dt><dd>{task.parameters.confidenceThreshold} / {task.parameters.iouThreshold}</dd></div><div><dt>最多目标数</dt><dd>{task.parameters.maxDetections}</dd></div>
            <div className="wide"><dt>模型版本</dt><dd className="monospace">{result.modelVersion}</dd></div><div className="wide"><dt>模型 SHA-256</dt><dd className="monospace">{result.output.modelSha256}</dd></div>
            <div className="wide"><dt>运行库 / 预处理</dt><dd>ONNX Runtime {result.output.runtimeVersion} · {result.output.preprocessingVersion}</dd></div>
          </dl>}
        </div>
        <div className="result-actions"><div className="download-actions"><button className="button button-quiet" type="button" onClick={downloadJson}><Icon name="download" size={16} />下载 JSON</button><button className="button button-quiet" type="button" disabled={downloading || !active} onClick={() => void downloadAnnotated()}><Icon name={downloading ? 'refresh' : 'download'} className={downloading ? 'spin' : undefined} size={16} />{downloading ? '正在读取框图…' : '下载框图 PNG'}</button></div></div>
        <ErrorNotice error={downloadError} title="框图下载失败" /><div className="action-feedback" role="status">{feedback}</div>
      </>}
      <div className="detail-footnote"><Icon name="clock" size={13} /><span>提交于 {dateTime(task.createdAt)} · 时间按本机时区显示</span></div>
    </>}
  </section>
}

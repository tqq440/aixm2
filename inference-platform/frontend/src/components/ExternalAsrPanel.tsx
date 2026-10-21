import type { useExternalAsr } from '../hooks/useExternalAsr'
import { dateTime } from '../utils/format'
import { hasUnknownUpstreamVersion } from '../utils/modelVersion'
import { ErrorNotice } from './Feedback'
import { Icon } from './Icon'
import { ExternalCapacityPanel } from './ExternalCapacityPanel'
import '../asr-service.css'

const labels = { unconfigured: '尚未配置', unavailable: '暂不可调用', ready: '可以转写', running: '正在调用', uncertain: '远端结果未知', stopping: '停止接单中' }

export function ExternalAsrPanel({ external, onSelectTask, restartRequired = false }: { external: ReturnType<typeof useExternalAsr>; onSelectTask: (id: string) => void; restartRequired?: boolean }) {
  const { status, error, loading, stale, refresh } = external
  const unconfigured = status?.state === 'unconfigured' && !error
  const trusted = !stale && !error
  const taskId = trusted ? status?.uncertainTaskId ?? status?.currentTaskId : null
  const label = restartRequired && trusted ? '待重启后接单' : unconfigured ? labels.unconfigured : trusted && status ? labels[status.state] : loading && !status ? '正在读取状态' : '状态未确认'
  return <section className="panel asr-service-panel external-asr-panel" aria-labelledby="external-asr-title">
    <div className="asr-service-heading"><div><span className="section-kicker">ASR STATUS</span><h2 id="external-asr-title">ASR 服务状态</h2></div><button type="button" className="button button-secondary" onClick={refresh} disabled={loading}><Icon name="refresh" size={15} className={loading ? 'spin' : undefined} />刷新调用状态</button></div>
    <div className="asr-service-state-line"><span className={`asr-service-state tone-${restartRequired ? 'warning' : trusted && status?.available ? 'success' : status?.state === 'uncertain' ? 'warning' : 'pending'}`}><span />{label}</span><span className="asr-service-agent">当前运行接口</span></div>
    <p className="asr-service-description">{error ? '无法确认服务调用状态，暂不提交新的接口转写任务。' : restartRequired ? '已选用新的服务配置，请重启平台后再提交新音频。下方仍展示当前进程的模型和调用记录。' : unconfigured ? '请在服务配置中填写接口、保存并选用，然后重启平台。上游模型版本可以暂不提供。' : !trusted ? '状态已过期或尚未确认，请刷新后再提交。' : status?.message}</p>
    {status?.modelId && <p className="asr-service-version">模型：{status.modelId} · {hasUnknownUpstreamVersion(status) ? '上游模型版本未知 · 接入修订' : '版本'}：<code>{status.modelVersion ?? '待提供'}</code></p>}
    {taskId && <div className="asr-service-task"><span>{status?.uncertainTaskId ? '待核对任务' : '当前任务'}</span><button type="button" className="text-button" onClick={() => onSelectTask(taskId)} aria-label={`查看外部任务 ${taskId}`}>{taskId.slice(0, 12)}…</button></div>}
    {trusted && status?.state === 'uncertain' && <p className="asr-service-last-error">请求可能仍在远端执行；本通道暂停接单。请管理员核对请求已结束后解除阻塞，页面不会自动重发。</p>}
    {status?.capacity && <ExternalCapacityPanel value={status.capacity} trusted={trusted} onSelectTask={onSelectTask} />}
    <p className="asr-service-observed">状态核对：{dateTime(status?.observedAt)}</p>
    <p className="asr-service-note">此处展示平台调用通道状态。模型服务由你或服务提供方部署和启动；连接测试不会启停模型。</p>
    <ErrorNotice error={error} title="服务调用状态读取失败" />
  </section>
}

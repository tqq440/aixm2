import type { useAsrScheduling } from '@/hooks/useAsrScheduling'
import type { AsrCatalogTarget, AsrResourceObservation } from '@/types/asrScheduling'
import { observationFreshness } from '@/utils/asrScheduling'
import { dateTime } from '@/utils/format'
import { ErrorNotice } from '@/components/Feedback'
import { Icon } from '@/components/Icon'
import '@/asr-scheduling.css'

const deviceNames = { unknown: '设备待核实', gpu: 'GPU 声明 · 待核实', cpu: 'CPU 声明 · 待核实' }
const healthNames = { ready: '报告就绪', unavailable: '报告不可用', unknown: '未报告就绪状态' }
const freshnessNames = { fresh: '采样未过期', stale: '采样已过期', unknown: '采样未确认' }

function Observation({ value, trusted, now }: { value: AsrResourceObservation; trusted: boolean; now: number }) {
  const freshness = observationFreshness(value, trusted, now)
  const current = freshness === 'fresh'
  const measured = (value: number | null, suffix = '') => current && value !== null ? `${value}${suffix}` : '未确认'
  return <div className="asr-observation">
    <div className="asr-observation-heading"><strong>{value.scope === 'target' ? '目标报告' : '共享组报告'}</strong><span className={`scheduling-chip ${current ? '' : 'is-warning'}`}>{freshnessNames[freshness]}</span></div>
    <p className="asr-observation-source">来源：{value.source} · {value.evidenceKind === 'isolated' ? '隔离测试资料' : '提供方资料'} · {current ? healthNames[value.reportedHealth] : '当前状态未确认'}</p>
    <dl className="asr-observation-metrics">
      <div><dt>报告可用并发</dt><dd>{measured(value.reportedAvailableConcurrency)}</dd></div>
      {value.gpuUtilizationPercent !== null && <div><dt>报告 GPU 利用率</dt><dd>{measured(value.gpuUtilizationPercent, '%')}</dd></div>}
      {value.gpuMemoryFreeMiB !== null && <div><dt>报告空闲显存</dt><dd>{measured(value.gpuMemoryFreeMiB, ' MiB')}</dd></div>}
      {value.cpuAvailableCores !== null && <div><dt>报告 CPU 余量</dt><dd>{measured(value.cpuAvailableCores, ' 核')}</dd></div>}
    </dl>
    <p className="asr-observation-time">采样：{dateTime(value.observedAt)} · 到期：{dateTime(value.expiresAt)}</p>
  </div>
}

function Target({ value, trusted, now }: { value: AsrCatalogTarget; trusted: boolean; now: number }) {
  return <article className="asr-catalog-target" aria-label={`目标 ${value.targetId}`}>
    <div className="asr-target-heading"><h4>{value.targetId}</h4><span className="scheduling-chip">{deviceNames[value.deviceKind]}</span></div>
    <p className="asr-target-meta">{value.modelId ? `模型 ${value.modelId} · ` : ''}共享组 {value.capacityGroupId}</p>
    <p className="asr-target-meta">平台配置限额：目标 {value.targetLimit} / 共享组 {value.groupLimit} · {value.mode === 'legacy' ? '原固定通道登记' : '目录登记'}</p>
    {value.observations.length ? <div className="asr-observation-grid">{value.observations.map(item => <Observation key={item.scope} value={item} trusted={trusted} now={now} />)}</div>
      : <p className="asr-target-empty">尚无资源报告，设备余量未知。登记成功不表示可以执行任务。</p>}
  </article>
}

export function AsrSchedulingPanel({ scheduling }: { scheduling: ReturnType<typeof useAsrScheduling> }) {
  const { status, targets, statusTrusted, targetsTrusted, statusError, targetsError, loading, offset, pageSize, now, refresh, setOffset } = scheduling
  const samePage = targets?.offset === offset
  const mode = statusTrusted ? status?.mode : null
  const label = mode === 'legacy' ? '原固定通道' : mode === 'isolated' ? '隔离调度验证' : mode === 'blocked' ? '调度等待核实' : '调度状态未确认'
  return <section className="panel asr-scheduling-panel" aria-labelledby="asr-scheduling-title">
    <div className="asr-service-heading"><div><span className="section-kicker">ASR SCHEDULING</span><h2 id="asr-scheduling-title">ASR 调度</h2></div><button className="button button-secondary" type="button" onClick={refresh} disabled={loading}><Icon name="refresh" size={15} className={loading ? 'spin' : undefined} />刷新调度信息</button></div>
    <div className="asr-scheduling-summary"><span className={`scheduling-chip ${mode === 'blocked' || !mode ? 'is-warning' : ''}`}>{label}</span><span>只读核对 · 不启停模型</span></div>
    <p className="asr-scheduling-message">{statusTrusted && status ? status.message : loading && !status ? '正在读取平台调度配置与目标目录。' : '无法确认当前调度状态，请刷新后核对。原任务不会因页面刷新重新提交。'}</p>
    {mode === 'legacy' && <p className="asr-scheduling-note">沿用原固定目标与平台并发限额，设备待核实。调用许可见上方“调用容量”；本机 Whisper 不是外部模型的 CPU 替代。</p>}
    {mode === 'isolated' && <p className="asr-scheduling-note">当前仅验证 {status?.candidateCount} 个隔离候选的选择流程，不代表实验室 GPU、CPU 或真实容量已接通。</p>}
    <ErrorNotice error={statusError} title="调度信息读取失败" />
    <details className="asr-target-directory">
      <summary><strong>目标与资源报告</strong><span>{targetsTrusted && targets ? `${targets.total} 项登记` : '目录待核对'}</span></summary>
      <p className="asr-scheduling-note">目录中的设备是声明，尚未核实。资源报告是有时效的采样值；平台并发许可、GPU 利用率与报告余量不能相互替代，也不因登记或报告就自动启用目标。</p>
      <ErrorNotice error={targetsError} title="目标目录读取失败" />
      {!targetsTrusted && <p className="asr-scheduling-warning" role="status">{loading && !targets ? '正在读取目标目录…' : '目标目录或采样状态未确认，上次报告不能作为当前可用资源。'}</p>}
      {samePage && targets?.items.map(item => <Target key={item.targetId} value={item} trusted={Boolean(targetsTrusted)} now={now} />)}
      {targetsTrusted && targets?.total === 0 && <p className="asr-target-empty">尚未登记计算目标。当前没有可供核对的设备与资源报告。</p>}
      {samePage && targets && targets.total > pageSize && <div className="asr-directory-pagination" aria-label="目标目录分页"><button type="button" className="button button-quiet" disabled={loading || offset === 0} onClick={() => setOffset(Math.max(0, offset - pageSize))}>上一页目标</button><span>第 {Math.floor(offset / pageSize) + 1} / {Math.max(1, Math.ceil(targets.total / pageSize))} 页</span><button type="button" className="button button-quiet" disabled={loading || offset + pageSize >= targets.total} onClick={() => setOffset(offset + pageSize)}>下一页目标</button></div>}
    </details>
    <details className="asr-scheduling-help"><summary>为什么任务还在等待？</summary><ul><li><strong>信息未知或过期：</strong>先补充并核对设备、兼容性与资源报告，不把缺少信息当作空闲。</li><li><strong>容量已占满：</strong>任务按原截止时间等待。GPU 满额后，CPU 仍须满足同模型支持、容量与性能条件。</li><li><strong>结果待核对：</strong>不会复制到 CPU 或其他目标重跑；是否仍占用许可，以调用记录和当前调用容量为准。</li><li><strong>调用通道停止或失联：</strong>确认调用池状态后再处理；外部模型的部署和启停由服务方负责。</li></ul></details>
    <p className="asr-service-observed">调度核对：{dateTime(status?.observedAt)} · 目录核对：{dateTime(targets?.observedAt)}</p>
  </section>
}

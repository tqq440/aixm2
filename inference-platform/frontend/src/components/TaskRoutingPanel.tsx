import { useEffect, useState } from 'react'
import type { TaskView } from '@/types/api'
import { snapshotFresh, validTaskRouting } from '@/utils/asrScheduling'
import { dateTime } from '@/utils/format'
import '@/asr-scheduling.css'

export function TaskRoutingPanel({ task, untrusted }: { task: TaskView; untrusted: boolean }) {
  const [now, setNow] = useState(Date.now())
  const route = task.routing
  useEffect(() => {
    if (!route || route.selectionState === 'selected') return
    const timer = window.setInterval(() => setNow(Date.now()), 1_000)
    return () => window.clearInterval(timer)
  }, [route])
  if (!route) return task.remoteOutcome != null ? <p className="task-routing-legacy">原固定通道任务：未记录 GPU / CPU 选择，继续遵守提交时的模型与连接配置。</p> : null
  if (!validTaskRouting(route)) return <div className="task-routing-panel" role="status"><h3>调度记录未确认</h3><p>调度记录格式不完整，请重新读取任务详情。</p></div>
  const selected = route.selectionState === 'selected'
  const unknown = task.remoteOutcome === 'unknown' || route.reasonCategory === 'outcome_unknown'
  const terminal = ['failed', 'cancelled', 'succeeded'].includes(task.status)
  const trusted = !untrusted && snapshotFresh(route.observedAt, now)
  const title = unknown ? '原目标结果待核对' : selected ? `已选择 ${route.deviceKind?.toUpperCase()} 目标`
    : terminal ? '任务已结束，未选择目标' : trusted ? '等待选择执行目标' : '调度原因未确认'
  const message = unknown ? '原请求结果尚未确认，保留原目标记录；不会自动转到 CPU 或其他目标重发。是否仍占用许可，请核对调用记录与当前调用容量。'
    : untrusted ? '任务详情暂时无法刷新；已保存的选择不代表目标当前可用。'
      : selected ? route.message ?? '此任务保留已经选择的目标，不会根据新资源报告重新改派。'
        : terminal ? route.message ?? '任务已经结束，不再安排目标选择。'
          : trusted ? route.message ?? '后台尚未确认满足条件的目标，请等待调度信息更新。'
            : '等待原因已过期或尚未读取；请刷新任务详情，不要重复提交。'
  return <section className={`task-routing-panel ${unknown ? 'is-warning' : ''}`} aria-label="任务调度说明">
    <div className="task-routing-heading"><h3>{title}</h3>{route.evidenceKind && <span className="scheduling-chip">{route.evidenceKind === 'isolated' ? '隔离验证记录' : '提供方资料 · 待核实'}</span>}</div>
    <p>{message}</p>
    {selected && <p className="task-routing-target">目标：<strong>{route.targetId}</strong> · 选择于 {dateTime(route.selectedAt)}</p>}
    {!selected && !terminal && trusted && Boolean(route.candidates?.length) && <details className="task-routing-candidates"><summary>查看候选等待原因</summary><ul>{route.candidates?.map(item => <li key={item.targetId}><strong>{item.targetId}</strong><span>{item.deviceKind === 'unknown' ? '设备未知' : `${item.deviceKind.toUpperCase()} 声明`} · {item.eligible ? '满足本次条件，尚未取得执行权' : '暂不满足条件'}</span><p>{item.message}</p></li>)}</ul></details>}
    {!selected && !terminal && <p className="asr-scheduling-note">等待资源不会增加执行次数，也不会延长任务原有期限；页面不代替后台做目标选择。</p>}
    {selected && <p className="asr-scheduling-note">目标选择记录与实际硬件利用率是不同信息；隔离记录不构成真实 GPU / CPU 验证。</p>}
  </section>
}

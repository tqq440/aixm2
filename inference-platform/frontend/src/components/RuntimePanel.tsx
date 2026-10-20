import type { ComponentView, RuntimeView, TaskType } from '../types/api'
import type { ErrorInfo } from './Feedback'
import { Icon } from './Icon'

function componentStatus(component: ComponentView | undefined, isWorker: boolean, taskType: TaskType = 'asr') {
  if (!component) return { tone: 'neutral', label: '等待状态', detail: '正在读取后台报告' }
  const availability = component.availability
  if (availability === 'stopped' || availability === 'not_started') return { tone: 'warning', label: '未运行', detail: isWorker ? taskType === 'asr' ? '在 ASR 服务管理区启动转写服务' : '启动 Worker 后自动处理任务' : '启动投递器后发送任务通知' }
  if (availability === 'stale') return { tone: 'warning', label: '报告已过期', detail: '暂时无法确认是否可用' }
  if (isWorker && ['draining', 'stopping', 'stop_unconfirmed'].includes(component.reportedState ?? '')) return { tone: 'warning', label: component.reportedState === 'stop_unconfirmed' ? '停止未确认' : '停止中', detail: '不再接单，等待当前任务收尾与退出确认' }
  if (availability === 'error') return { tone: 'danger', label: '运行异常', detail: component.lastError || '请检查对应程序的日志' }
  if (isWorker && !component.modelReady) return { tone: 'pending', label: '模型加载中', detail: '加载完成后开始接收任务' }
  if (isWorker && component.reportedState === 'busy') return { tone: 'pending', label: taskType === 'vision' ? '正在检测' : '正在转写', detail: '一次执行 1 个任务' }
  if (component.ready) return { tone: 'success', label: isWorker ? '就绪' : '运行中', detail: isWorker ? '模型已加载 · 等待任务' : '自动投递待处理任务' }
  return { tone: 'neutral', label: '准备中', detail: '等待下一次运行报告' }
}

export function RuntimePanel({ runtime, error, loading, taskType = 'asr' }: { runtime: RuntimeView | null; error: ErrorInfo | null; loading: boolean; taskType?: TaskType }) {
  const unavailable = { tone: 'neutral', label: '状态未知', detail: '后台状态暂时不可读取' }
  const worker = error ? unavailable : componentStatus(taskType === 'vision' ? runtime?.visionWorker : runtime?.worker, true, taskType)
  const dispatcher = error ? unavailable : componentStatus(runtime?.dispatcher, false)
  const api = error
    ? { tone: 'danger', label: '连接异常', detail: '检查 API 是否启动或稍后刷新' }
    : runtime ? { tone: 'success', label: '已连接', detail: '本机服务 · 实时状态' }
      : { tone: 'neutral', label: loading ? '连接中' : '等待连接', detail: '正在读取后台状态' }
  return <section className="runtime-grid" aria-label="运行状态">
    {[{ title: '后台接口', icon: 'server' as const, state: api }, { title: taskType === 'vision' ? '图片 Worker' : '转写 Worker', icon: taskType === 'vision' ? 'image' as const : 'wave' as const, state: worker }, { title: '通知投递器', icon: 'grid' as const, state: dispatcher }].map(({ title, icon, state }) => <div className="runtime-card" key={title}>
      <div className={`runtime-icon tone-${state.tone}`}><Icon name={icon} size={21} /></div>
      <div className="runtime-content"><div className="runtime-line"><span>{title}</span><span className={`live-dot tone-${state.tone}`} /><strong>{state.label}</strong></div><p title={state.detail}>{state.detail}</p></div>
    </div>)}
  </section>
}

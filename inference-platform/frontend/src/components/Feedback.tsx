import { Icon } from './Icon'

export interface ErrorInfo { message: string; requestId?: string | null; code?: string }

export function ErrorNotice({ error, title }: { error: ErrorInfo | null; title?: string }) {
  if (!error) return null
  return <div className="error-notice" role="alert">
    <Icon name="warning" size={18} />
    <div>{title && <strong>{title}</strong>}<p>{error.message}</p>{error.requestId && <small>请求编号：{error.requestId}</small>}</div>
  </div>
}

const labels = { created: '待投递', queued: '排队中', running: '转写中', retry_wait: '等待重试', cancel_requested: '取消中', succeeded: '已完成', failed: '失败', cancelled: '已取消' } as const

export function StatusBadge({ status, taskType = 'asr' }: { status: keyof typeof labels; taskType?: 'asr' | 'vision' }) {
  return <span className={`status-badge status-${status}`}><span />{status === 'running' && taskType === 'vision' ? '检测中' : labels[status]}</span>
}

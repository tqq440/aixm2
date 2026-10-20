import type { ManagementStatus } from '../types/management'

/** Display gating only. The backend still arbitrates admission atomically. */
export function asrSubmissionBlock(status: ManagementStatus | null, readFailed: boolean, pending: 'start' | 'stop' | null): string | null {
  if (pending === 'stop') return '正在发送停止请求，暂不提交新音频。'
  if (readFailed || !status) return 'ASR 服务状态尚未确认，请先读取服务状态。'
  if (!status.agentAvailable) return '节点 Agent 不可用，无法确认 ASR 服务状态。'
  if (status.state === 'stop_unconfirmed') return 'ASR 停止尚未确认，请检查服务状态与日志。'
  if (status.state === 'stopped') return 'ASR 服务未启动，请先在服务管理区启动。'
  if (status.state === 'failed') return 'ASR 服务启动或运行失败，请查看最近错误。'
  if (status.state === 'unknown') return 'ASR 服务状态尚未确认，请先读取服务状态。'
  if (status.state === 'stopping' || status.desiredState === 'stopped') return 'ASR 正在停止，当前任务收尾后退出。'
  if (status.state === 'starting') return 'ASR 模型正在加载，请等待就绪。'
  if (!status.modelReady || !status.acceptingTasks || !['ready', 'busy'].includes(status.state)) return 'ASR 暂时不能接收新任务，请等待状态确认。'
  return null
}

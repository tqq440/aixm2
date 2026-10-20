import type { TaskType } from '../types/api'

export const TASK_ID = /^[0-9a-f]{32}$/
export type WorkspaceType = TaskType | 'llm'

/** ASR keeps its existing ?task= links; Vision is selected explicitly. */
export function readWorkspaceUrl(): { taskType: WorkspaceType; taskId: string | null } {
  const query = new URL(window.location.href).searchParams
  const taskId = query.get('task')
  return {
    taskType: query.get('type') === 'llm' ? 'llm' : query.get('type') === 'vision' ? 'vision' : 'asr',
    taskId: taskId && TASK_ID.test(taskId) ? taskId : null,
  }
}

export function writeWorkspaceUrl(taskType: WorkspaceType, taskId: string | null, push = false): void {
  const url = new URL(window.location.href)
  if (taskType !== 'asr') url.searchParams.set('type', taskType)
  else url.searchParams.delete('type')
  const key = taskType === 'llm' ? 'request' : 'task'
  url.searchParams.delete(taskType === 'llm' ? 'task' : 'request')
  if (taskId && TASK_ID.test(taskId)) url.searchParams.set(key, taskId)
  else url.searchParams.delete(key)
  if (url.href === window.location.href) return
  window.history[push ? 'pushState' : 'replaceState'](window.history.state, '', url)
}

export function readOnlineRequestId(): string | null {
  const query = new URL(window.location.href).searchParams
  const id = query.get('request')
  return query.get('type') === 'llm' && id && TASK_ID.test(id) ? id : null
}

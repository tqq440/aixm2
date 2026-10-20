import type { AnyTaskView, TaskPage, TaskType } from '../types/api'
import { dateTime, seconds } from '../utils/format'
import { ErrorNotice, StatusBadge } from './Feedback'
import type { ErrorInfo } from './Feedback'
import { Icon } from './Icon'

interface Props { tasks: TaskPage<AnyTaskView> | null; error: ErrorInfo | null; loading: boolean; selectedId: string | null; page: number; onPage: (page: number) => void; onSelect: (id: string) => void; taskType?: TaskType }

export function TaskList({ tasks, error, loading, selectedId, page, onPage, onSelect, taskType = 'asr' }: Props) {
  const vision = taskType === 'vision'
  const lastPage = Math.max(1, Math.ceil((tasks?.total ?? 0) / (tasks?.pageSize ?? 20)))
  return <section className="panel task-panel" aria-label="任务列表" aria-busy={loading}>
    <div className="panel-heading task-heading"><div className="heading-with-count"><h2>任务列表</h2>{tasks && <span className="count-pill">{tasks.total}</span>}</div><span className="polling-note"><span className="tiny-dot" />进行中的任务每 2 秒更新</span></div>
    <ErrorNotice error={error} title="任务列表暂时不可用" />
    {!tasks && !error && <div className="loading-state"><Icon name="refresh" className="spin" /><span>正在读取任务…</span></div>}
    {tasks?.items.length === 0 && <div className="empty-state list-empty"><span className="empty-icon"><Icon name={vision ? 'image' : 'audio'} size={30} /></span><h3>{vision ? '从第一张图片开始' : '从第一段音频开始'}</h3><p>{vision ? '提交图片后，检测状态和结果会出现在这里。' : '提交音频后，处理状态和结果会出现在这里。'}</p></div>}
    {tasks && tasks.items.length > 0 && <>
      <div className="task-columns" aria-hidden="true"><span>{vision ? '图片文件' : '音频文件'}</span><span>状态</span><span>提交时间</span><span /></div>
      <div className="task-rows">{tasks.items.map(task => <button type="button" className={`task-row ${task.taskId === selectedId ? 'is-selected' : ''}`} key={task.taskId} aria-label={`查看任务 ${task.input.fileName} ${task.taskId.slice(0, 8)}`} aria-pressed={task.taskId === selectedId} onClick={() => onSelect(task.taskId)}>
        <span className="task-file"><span className="file-symbol"><Icon name={task.taskType === 'vision' ? 'image' : 'audio'} size={20} /></span><span className="task-file-text"><strong title={task.input.fileName}>{task.input.fileName}</strong><small>{task.taskType === 'vision' ? `${task.input.width} × ${task.input.height}` : seconds(task.input.durationSeconds)}<span className="meta-separator">·</span>{task.modelId}</small>{task.error && <span className="row-error" title={task.error.message}>{task.error.message}</span>}</span></span>
        <span className="task-state"><StatusBadge status={task.status} taskType={task.taskType} /></span>
        <time className="task-date" dateTime={task.createdAt}>{dateTime(task.createdAt)}</time>
        <Icon name="chevron" size={16} className="row-chevron" />
      </button>)}</div>
      <div className="list-footer"><span>共 {tasks.total} 个任务 · 第 {page} / {lastPage} 页</span><div><button type="button" className="page-button" disabled={page <= 1 || loading} onClick={() => onPage(page - 1)} aria-label="上一页">上一页</button><button type="button" className="page-button" disabled={page >= lastPage || loading} onClick={() => onPage(page + 1)} aria-label="下一页">下一页</button></div></div>
    </>}
  </section>
}

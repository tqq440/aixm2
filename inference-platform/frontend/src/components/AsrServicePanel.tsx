import { useEffect, useState } from 'react'
import type { useAsrManagement } from '../hooks/useAsrManagement'
import { dateTime } from '../utils/format'
import { ErrorNotice } from './Feedback'
import { Icon } from './Icon'
import '../asr-service.css'

type Management = ReturnType<typeof useAsrManagement>

interface Props {
  management: Management
  active: boolean
  onSelectTask: (taskId: string) => void
}

const states = {
  stopped: { label: '未启动', tone: 'neutral', detail: '启动服务并等待模型就绪后，即可提交转写。' },
  starting: { label: '模型加载中', tone: 'pending', detail: '正在准备转写模型，请等待就绪；此时尚不接收新任务。' },
  ready: { label: '可以转写', tone: 'success', detail: '模型已加载，可以提交音频。' },
  busy: { label: '正在执行', tone: 'pending', detail: '模型已加载，正在处理当前音频；新任务进入原有队列。' },
  stopping: { label: '停止中', tone: 'warning', detail: '已停止接收新任务，等待当前音频结束并确认进程退出。' },
  stop_unconfirmed: { label: '停止未确认', tone: 'danger', detail: '尚未确认进程退出。请检查 Agent 和日志；暂不能再次启动。' },
  failed: { label: '启动或运行失败', tone: 'danger', detail: '请查看最近错误，处理原因后重试启动。' },
  unknown: { label: '状态未确认', tone: 'neutral', detail: '暂时无法确认服务状态。请恢复连接后刷新；连接中断不表示服务已停止。' },
} as const

export function AsrServicePanel({ management, active, onSelectTask }: Props) {
  const [token, setToken] = useState('')
  const { nodes, instances, selectedNodeId, selectedInstanceId, selectedInstance, status } = management
  // A management secret belongs only to this visible ASR context, never browser storage.
  useEffect(() => { setToken('') }, [active, selectedNodeId, selectedInstanceId])
  useEffect(() => {
    const clear = () => setToken('')
    window.addEventListener('pagehide', clear)
    return () => window.removeEventListener('pagehide', clear)
  }, [])
  if (!active) return null

  const trusted = Boolean(status && !management.error)
  const available = Boolean(trusted && status?.agentAvailable && status.agentSessionId)
  let state: keyof typeof states = trusted && status ? status.state : 'unknown'
  if (!available && state !== 'stopped' && state !== 'stop_unconfirmed') state = 'unknown'
  if ((state === 'ready' || state === 'busy') && (!status?.modelReady || status.desiredState === 'stopped')) state = 'unknown'
  if (state === 'ready' && !status?.acceptingTasks) state = 'unknown'
  const pending = management.commandPending !== null
  const view = pending
    ? { label: '命令发送中', tone: 'pending', detail: '正在发送管理命令，请等待后台确认服务状态。' }
    : management.loading && !status && !management.error
      ? { label: '正在核对状态', tone: 'neutral', detail: '正在读取节点与服务报告，请稍候。' }
      : states[state]
  const secret = token.trim()
  const hasToken = secret.length >= 32 && secret.length <= 256
  const canManage = available && !pending && hasToken && selectedInstance?.managementMode === 'node_agent'
  const canStart = canManage && (state === 'stopped' || state === 'failed')
  const canStop = canManage && (state === 'starting' || state === 'ready' || state === 'busy')
  const node = nodes.find(item => item.nodeId === selectedNodeId)
  const currentTaskKnown = available && state !== 'unknown' && state !== 'stop_unconfirmed'
  const activeTask = currentTaskKnown ? status?.activeTaskId : null
  const detail = state === 'busy' && !status?.acceptingTasks
    ? '正在处理当前音频，当前不接收新任务。请等待最新状态。'
    : view.detail

  return <section className="panel asr-service-panel" aria-labelledby="asr-service-title">
    <div className="asr-service-heading">
      <div className="asr-service-title-group"><span className="asr-service-symbol"><Icon name="server" size={22} /></span><div><span className="section-kicker">本机 WHISPER · NODE AGENT</span><h2 id="asr-service-title">ASR 服务管理</h2></div></div>
      <button className="button button-quiet" type="button" disabled={management.loading || pending} onClick={management.refresh}><Icon name="refresh" size={15} className={management.loading ? 'spin' : undefined} />刷新服务状态</button>
    </div>

    <div className="asr-service-layout">
      <div className="asr-service-selection">
        <div className="asr-service-selects">
          <label><span className="field-label">计算节点</span><select aria-label="计算节点" value={selectedNodeId ?? ''} disabled={pending || !nodes.length} onChange={event => management.selectNode(event.target.value)}>
            {!nodes.length && <option value="">{management.loading ? '正在读取节点…' : '暂无已登记节点'}</option>}
            {nodes.map(item => <option value={item.nodeId} key={item.nodeId}>{item.hostname}</option>)}
          </select></label>
          <label><span className="field-label">ASR 实例</span><select aria-label="ASR 实例" value={selectedInstanceId ?? ''} disabled={pending || !instances.length} onChange={event => management.selectInstance(event.target.value)}>
            {!instances.length && <option value="">{management.loading ? '正在读取实例…' : '暂无 ASR 实例'}</option>}
            {instances.map(item => <option value={item.instanceId} key={item.instanceId}>{item.instanceKey} · {item.modelId}</option>)}
          </select></label>
        </div>
        {selectedInstance && <p className="asr-service-model"><strong>{selectedInstance.modelId}</strong><span>CPU</span><span>{selectedInstance.managementMode === 'node_agent' ? 'Agent 管理' : '尚未交由 Agent 管理'}</span></p>}
        {selectedInstance && <p className="asr-service-version">模型版本 <code>{selectedInstance.modelVersion}</code></p>}
        {node && <p className="asr-service-node">{node.osName} · {node.architecture}{node.cpuCount != null ? ` · ${node.cpuCount} 个逻辑 CPU` : ''} · GPU 未探测</p>}
        {!selectedInstance && !management.loading && !management.error && <p className="asr-service-note">登记信息不代表模型已运行。请先按操作说明登记本机节点与实例。</p>}
      </div>

      <div className="asr-service-current" aria-live="polite">
        <div className="asr-service-state-line"><span className={`asr-service-state tone-${view.tone}`}><span />{view.label}</span><span className="asr-service-agent">{available ? 'Agent 已连接' : management.loading || pending ? '正在核对 Agent' : 'Agent 暂不可用'}</span></div>
        <p className="asr-service-description">{detail}</p>
        <div className="asr-service-task"><span>当前任务</span>{activeTask
          ? <button type="button" className="text-button" title={activeTask} aria-label={`查看当前任务 ${activeTask}`} onClick={() => onSelectTask(activeTask)}>{activeTask.slice(0, 12)}…<Icon name="arrow" size={13} /></button>
          : <span>{currentTaskKnown ? '无正在执行的任务' : '暂无法确认'}</span>}</div>
        <p className="asr-service-observed">状态核对：{trusted ? dateTime(status?.evaluatedAt) : '等待后台确认'}</p>
      </div>
    </div>

    <div className="asr-service-controls">
      <div className="asr-service-token">
        <label className="field-label" htmlFor="asr-management-token">本机管理令牌</label>
        <div className="asr-service-token-input"><input id="asr-management-token" type="password" value={token} onChange={event => setToken(event.target.value)} autoComplete="off" spellCheck={false} maxLength={256} disabled={pending} aria-describedby="asr-management-token-help" placeholder="仅启停服务时需要" /><button type="button" className="text-button" disabled={!token || pending} onClick={() => setToken('')}>清空令牌</button></div>
      </div>
      <div className="asr-service-actions">
        <button type="button" className="button button-primary" disabled={!canStart} onClick={() => { void management.start(secret) }}><Icon name={management.commandPending === 'start' ? 'refresh' : 'wave'} size={16} className={management.commandPending === 'start' ? 'spin' : undefined} />{management.commandPending === 'start' ? '正在发送启动…' : '启动 ASR'}</button>
        <button type="button" className="button button-secondary" disabled={!canStop} onClick={() => { void management.stop(secret) }}><Icon name={management.commandPending === 'stop' ? 'refresh' : 'close'} size={16} className={management.commandPending === 'stop' ? 'spin' : undefined} />{management.commandPending === 'stop' ? '正在发送停止…' : '停止 ASR'}</button>
      </div>
    </div>
    <p id="asr-management-token-help" className="asr-service-note">从本机 <code>.env</code> 复制 <code>INFERENCE_AGENT_TOKEN</code> 的值，不要发送到聊天。令牌仅保存在当前页面内存，切换工作台、节点或实例、刷新页面后清除。</p>
    {token && !hasToken && <p className="asr-service-note asr-service-token-warning">请填写 32–256 个字符的管理令牌。</p>}
    <p className="asr-service-note asr-service-stop-note"><Icon name="info" size={13} />停止后不接收新任务，当前音频处理结束后退出。后台接口、图片检测和历史结果仍可使用。</p>
    {management.commandNotice && <p className="asr-service-command-notice" role="status">{management.commandNotice}</p>}
    <ErrorNotice error={management.error} title="服务状态读取失败" />
    <ErrorNotice error={management.commandError} title="管理操作未确认" />
    {trusted && status?.lastError && <div className="asr-service-last-error"><Icon name="warning" size={15} /><p><strong>最近错误</strong>{status.lastError}</p></div>}
  </section>
}

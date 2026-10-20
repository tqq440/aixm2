import type { ExternalAsrCapacity } from '../types/api'

const states: Record<string, string> = { not_started: '未启动', starting: '连接检查中', ready: '可领取任务', idle: '可领取任务', running: '正在执行', busy: '正在执行', committing: '保存结果中', stopping: '停止接单中', draining: '停止接单中', stopped: '已停止', unavailable: '服务不可达', error: '调用槽异常', stale: '心跳已过期', lost: '旧会话已失联', configuration_changed: '配置已变更' }

export function ExternalCapacityPanel({ value, trusted, onSelectTask }: {
  value: ExternalAsrCapacity; trusted: boolean; onSelectTask: (id: string) => void
}) {
  const age = Date.now() - Date.parse(value.observedAt)
  const fresh = trusted && age >= -5_000 && age < 10_000
  const number = (n: number) => fresh ? n : '—'
  const unknown = value.groupUncertain > 0 || value.legacyUnknownCount > 0 || value.activeCalls.some(call => !call.ownerFresh)
  return <div className="external-capacity" aria-label="外部调用容量">
    <div className="external-capacity-heading"><h3>调用容量</h3><span>设备：{value.deviceKind === 'unknown' ? '未知' : value.deviceKind.toUpperCase()}</span></div>
    {!fresh && <p role="status">容量状态未确认，请刷新；上次计数不代表当前可用容量。</p>}
    {!value.registered ? <p>调用目标尚未登记。</p> : <>
      <p className="external-capacity-identity">目标 <code>{value.targetId}</code> · 共享组 <code>{value.capacityGroup}</code></p>
      <dl className="external-capacity-metrics">
        <div><dt>目标占用 / 限额</dt><dd>{number(value.held + value.uncertain)} / {value.targetLimit}</dd></div>
        <div><dt>共享组占用 / 限额</dt><dd>{number(value.groupHeld + value.groupUncertain)} / {value.groupLimit}</dd></div>
        <div><dt>剩余调用配额</dt><dd>{number(value.remainingCapacity)}</dd></div>
        <div><dt>等待执行</dt><dd>{number(value.queued)}</dd></div>
      </dl>
      <p className="asr-service-note">限额仅控制本平台的并发请求，不代表 GPU 利用率或服务器最大性能。有剩余配额仍需调用槽就绪；满额时任务按原期限排队。</p>
      {fresh && value.incompatibleQueued > 0 && <p role="status">另有 {value.incompatibleQueued} 个任务等待原配置，不会改用新地址执行。</p>}
      {fresh && unknown && <p className="asr-service-last-error" role="status">有远端请求结果待核对，继续占用许可；不会自动重发到其他槽或设备。未确认占位：{value.groupUncertain}；旧记录待核对：{value.legacyUnknownCount}。</p>}
      <ul className="external-capacity-slots" aria-label="调用槽状态">{value.slots.map(slot => <li key={slot.slotIndex}>
        <div><strong>调用槽 {slot.slotIndex + 1}</strong><span>{fresh ? ['ready', 'idle'].includes(slot.state) && value.remainingCapacity === 0 ? '空闲，等待配额' : states[slot.state] ?? '状态未确认' : '状态未确认'}</span></div>
        {fresh && slot.currentTaskId && <button type="button" className="text-button" onClick={() => onSelectTask(slot.currentTaskId!)} aria-label={`查看调用槽 ${slot.slotIndex + 1} 的任务`}>{slot.currentTaskId.slice(0, 12)}…</button>}
        {fresh && slot.lastError && <p className="asr-service-last-error">最近错误：<code>{slot.lastError}</code></p>}
      </li>)}</ul>
      {fresh && value.activeCalls.length > 0 && <div className="external-capacity-calls"><h4>活动与待核对请求</h4><ul>{value.activeCalls.map(call => <li key={call.taskId}><button type="button" className="text-button" onClick={() => onSelectTask(call.taskId)}>{call.taskId.slice(0, 12)}…</button><span>{call.state === 'uncertain' || !call.ownerFresh ? '结果待核对' : '执行中'} · {call.targetId} / 槽 {call.slotIndex + 1}</span></li>)}</ul>{value.activeCallsTruncated && <p>显示前 50 条，共 {value.activeCallsTotal} 条；完整记录请使用本机维护查询。</p>}</div>}
    </>}
  </div>
}

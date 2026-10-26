import { useEffect, useState } from 'react'
import { ApiError } from '@/api/client'
import { validationIssuesApi } from '@/api/validation-issues'
import type { DeferralCode, EnvironmentIssuePage, ValidationStage } from '@/api/validation-issues'
import '@/gateway.css'

const stages = { module6a: '6A · 部署准备', module6b: '6B · Redis 验证' }
const reasons: Record<DeferralCode, string> = {
  CAMPUS_NETWORK_UNAVAILABLE: '校园网暂不可达，真实联调待网络恢复。',
  REDIS_CLIENT_MISSING: 'Redis 客户端尚未准备，真实队列验收待补。',
  REDIS_SERVER_UNVERIFIED: 'Redis 服务尚未核实，连接及重启验收待补。',
  LINUX_RUNTIME_UNVERIFIED: 'Linux 运行环境尚未核实，部署验收待补。',
}
const time = (value: string) => new Date(value).toLocaleString()

export function ValidationIssuesPanel() {
  const [stage, setStage] = useState<ValidationStage | ''>('')
  const [offset, setOffset] = useState(0)
  const [refresh, setRefresh] = useState(0)
  const [page, setPage] = useState<EnvironmentIssuePage | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  useEffect(() => {
    let active = true
    const controller = new AbortController()
    setLoading(true); setError(null)
    void validationIssuesApi.list(stage, offset, controller.signal).then(value => {
      if (active) setPage(value)
    }).catch(reason => {
      if (active) setError(reason instanceof ApiError && reason.status === 404
        ? '验收记录接口尚未加载，请在后台下次正常重启后查看。'
        : '验收记录暂时无法读取，当前状态尚不能确认。')
    }).finally(() => { if (active) setLoading(false) })
    return () => { active = false; controller.abort() }
  }, [stage, offset, refresh])

  return <section className="gateway-card validation-issues" aria-labelledby="validation-heading" aria-busy={loading}>
    <div className="validation-heading"><div><h2 id="validation-heading">环境与待验收记录</h2><p>环境问题不改变任务结果。刷新只读取已保存记录，不连接服务器。</p></div>
      <button type="button" className="button button-secondary" disabled={loading} onClick={() => setRefresh(value => value + 1)}>刷新验收记录</button></div>
    <div className="gateway-filters"><label>验收阶段<select value={stage} onChange={event => { setStage(event.target.value as ValidationStage | ''); setOffset(0); setPage(null) }}>
      <option value="">全部阶段</option><option value="module6a">6A · 部署准备</option><option value="module6b">6B · Redis 验证</option>
    </select></label><span className="validation-note">记录为空也不代表验收已通过。</span></div>
    {loading && <p role="status">正在读取本地验收记录…</p>}
    {error && <p role="alert" className="gateway-alert">{error}{page && ' 下方保留上次读取的记录。'}</p>}
    {page && <>
      {page.unreadableCount > 0 && <p role="status" className="gateway-alert">有 {page.unreadableCount} 份记录无法读取，清单可能不完整，请在本机核对。</p>}
      {page.scanTruncated && <p role="status" className="gateway-alert">本次读取已达到扫描上限，清单并不完整，请在本机核对剩余记录。</p>}
      {!page.items.length && !loading && <p>当前筛选下没有可展示的本地记录，实际验收状态仍需核实。</p>}
      <div className="validation-list">{page.items.map(issue => <article key={`${issue.stage}-${issue.target}`} className="validation-item">
        <div className="validation-item-title"><h3>{issue.target}</h3><span className="validation-pending">待外部验收</span></div>
        <p>{reasons[issue.code]}</p><small>{stages[issue.stage]} · {issue.source === 'operator' ? '人工确认' : '平台检查'} · 最近记录：{time(issue.lastObservedAt)}</small>
        <details><summary>查看记录信息</summary><dl><dt>原因代码</dt><dd><code>{issue.code}</code></dd><dt>首次记录</dt><dd>{time(issue.firstObservedAt)}</dd><dt>观察次数</dt><dd>{issue.observationCount}</dd><dt>证据哈希</dt><dd><code>{issue.evidenceSha256 || '未提供'}</code></dd></dl></details>
      </article>)}</div>
      {(page.total > 50 || offset > 0) && <div className="gateway-filters"><button type="button" disabled={loading || offset === 0} onClick={() => { setOffset(value => value - 50); setPage(null) }}>上一页验收记录</button>
        <span>第 {Math.floor(offset / 50) + 1} 页，共 {page.total} 条已读取记录</span><button type="button" disabled={loading || offset + 50 >= page.total} onClick={() => { setOffset(value => value + 50); setPage(null) }}>下一页验收记录</button></div>}
    </>}
  </section>
}

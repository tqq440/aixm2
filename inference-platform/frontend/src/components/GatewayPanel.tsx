import { useEffect, useState } from 'react'
import { gatewayApi } from '@/api/gateway'
import type { GatewayApplication, GatewayCapacity, GatewayEvents, GatewayMonitor, GatewayNode, GatewayRequest } from '@/api/gateway'
import '@/gateway.css'
import { ValidationIssuesPanel } from '@/components/ValidationIssuesPanel'

const labels: Record<string, string> = { waiting: '排队中', running: '执行中', succeeded: '已完成', failed: '失败', cancelled: '已取消', unknown: '结果未知' }
const budgetReasons: Record<string, string> = { GLOBAL_QUERY_LIMIT: '全平台查询名额已满，等待下次采集', SERVICE_QUERY_LIMIT: '该服务已有查询，等待下次采集', MONITOR_BUDGET_UNAVAILABLE: '中心查询许可暂不可用，保留上次可信进度', MONITOR_RELEASE_PENDING: '查询已结束，等待中心确认归还名额', MONITOR_QUERY_UNCONFIRMED: '上次查询是否结束尚未确认，保留查询占位' }
const time = (value?: string | null) => value ? new Date(value).toLocaleString() : '未提供'
const device = (value?: { kind: string; deviceId?: string | null } | null) => !value || value.kind === 'unknown' ? '未知' : `${value.kind.toUpperCase()}${value.deviceId ? ` ${value.deviceId}` : ''}`

export function GatewayPanel() {
  const [status, setStatus] = useState<{ enabled: boolean; managementAvailable: boolean; notice: string } | null>(null)
  const [apps, setApps] = useState<GatewayApplication[]>([])
  const [items, setItems] = useState<GatewayRequest[]>([])
  const [nodes, setNodes] = useState<GatewayNode[]>([])
  const [capacity, setCapacity] = useState<GatewayCapacity | null>(null)
  const [filter, setFilter] = useState('')
  const [serviceFilter, setServiceFilter] = useState('')
  const [page, setPage] = useState(1)
  const [total, setTotal] = useState(0)
  const [selected, setSelected] = useState<GatewayRequest | null>(null)
  const [monitor, setMonitor] = useState<GatewayMonitor | null>(null)
  const [events, setEvents] = useState<GatewayEvents | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [secret, setSecret] = useState<string | null>(null)
  const [appId, setAppId] = useState('')
  const [services, setServices] = useState('')
  const [limit, setLimit] = useState(1)
  const [enabled, setEnabled] = useState(true)
  const [version, setVersion] = useState<number | null>(null)
  const refresh = async () => {
    const state = await gatewayApi.status(); setStatus(state)
    if (!state.managementAvailable) return
    const [applications, requests, nodePage, resources] = await Promise.all([gatewayApi.applications(), gatewayApi.requests(filter, page, serviceFilter), gatewayApi.nodes(), gatewayApi.capacity()])
    setApps(applications.items); setItems(requests.items); setTotal(requests.total); setNodes(nodePage.items); setCapacity(resources)
    if (selected) setSelected(requests.items.find(item => item.id === selected.id) || selected)
  }
  const act = async (action: () => Promise<unknown>) => {
    setBusy(true); setError(null)
    try { await action() } catch (e) { setError(e instanceof Error ? e.message : '操作失败，请重试。') } finally { setBusy(false) }
  }
  useEffect(() => { let active = true; void refresh().catch(e => { if (active) setError(String(e.message || e)) }); return () => { active = false } }, [filter, page, serviceFilter]) // 只读平台缓存，刷新不查询模型。
  useEffect(() => {
    let active = true
    setMonitor(null); setEvents(null)
    if (selected) void Promise.all([gatewayApi.monitor(selected.invocationId), gatewayApi.events(selected.invocationId)]).then(([m, e]) => { if (active) { setMonitor(m); setEvents(e) } }).catch(e => { if (active) setError(e.message) })
    return () => { active = false }
  }, [selected])
  const edit = (app?: GatewayApplication) => {
    setAppId(app?.applicationId || ''); setServices(app?.allowedServices.join(', ') || ''); setLimit(app?.concurrencyLimit || 1); setEnabled(app?.enabled ?? true); setVersion(app?.version || null); setSecret(null)
  }
  return <section className="gateway-panel" aria-labelledby="gateway-heading">
    <div className="page-heading"><div><h1 id="gateway-heading">应用与任务监控</h1><p>管理调用凭据、服务权限和额度，查看各 Node 的任务状态。</p></div><button type="button" className="button button-secondary" disabled={busy} onClick={() => void act(refresh)}>刷新平台记录</button></div>
    {error && <div role="alert" className="gateway-alert">{error}</div>}
    {status && <p role="status">{status.notice}{!status.enabled && ' 公共调用当前关闭。'}</p>}
    <ValidationIssuesPanel />
    {status?.managementAvailable && <>
      <section className="gateway-card" aria-labelledby="applications-heading"><h2 id="applications-heading">应用与 API Key</h2><p>换 Key 不改变任务归属。轮换后旧 Key 立即失效；停用应用会拒绝新调用。</p>
        <form onSubmit={e => { e.preventDefault(); void act(async () => { await gatewayApi.save({ applicationId: appId, allowedServices: services.split(',').map(s => s.trim()).filter(Boolean), concurrencyLimit: limit, enabled }, version); edit(); await refresh() }) }}>
          <label>应用编号<input value={appId} disabled={version !== null} required pattern="[A-Za-z0-9][A-Za-z0-9._-]*" maxLength={64} onChange={e => setAppId(e.target.value)} /></label>
          <label>允许服务（英文逗号分隔）<input required value={services} onChange={e => setServices(e.target.value)} placeholder="campus-asr, campus-yolo" /></label>
          <label>并发额度<input type="number" min={1} max={64} required value={limit} onChange={e => setLimit(Number(e.target.value))} /></label>
          <label><input type="checkbox" checked={enabled} onChange={e => setEnabled(e.target.checked)} />启用应用</label>
          <button className="button button-primary" disabled={busy}>{version ? '保存应用修改' : '登记应用'}</button><button type="button" className="button button-quiet" onClick={() => edit()}>清空表单</button>
        </form>
        {secret && <div className="gateway-secret" role="status"><strong>完整 Key 仅显示本次，请安全保存。</strong><code>{secret}</code><button type="button" className="button button-secondary" onClick={() => setSecret(null)}>隐藏并清除</button></div>}
        <div className="gateway-scroll"><table><thead><tr><th>应用</th><th>服务 / 额度</th><th>凭据与操作</th></tr></thead><tbody>{apps.filter(a => a.applicationId !== 'local-user').map(app => <tr key={app.applicationId}><td>{app.applicationId}<small>{app.enabled ? '已启用' : '已停用'}</small><button type="button" disabled={busy} onClick={() => edit(app)}>编辑</button></td><td>{app.allowedServices.join(', ')}<small>最多 {app.concurrencyLimit} 个同时执行</small></td><td><button type="button" disabled={busy} onClick={() => void act(async () => { setSecret(null); const result = await gatewayApi.issue(app.applicationId); setSecret(result.apiKey); await refresh() })}>创建 Key</button>{app.keys.map(key => <div key={key.keyId}><code>{key.keyId.slice(0, 8)}…</code> {key.revokedAt ? '已撤销' : <><button type="button" disabled={busy} onClick={() => void act(async () => { setSecret(null); const result = await gatewayApi.rotate(app.applicationId, key.keyId); setSecret(result.apiKey); await refresh() })}>轮换</button><button type="button" disabled={busy} onClick={() => void act(async () => { setSecret(null); await gatewayApi.revoke(app.applicationId, key.keyId); await refresh() })}>撤销</button></>}</div>)}</td></tr>)}</tbody></table></div>
      </section>
      <section className="gateway-card" aria-labelledby="requests-heading"><h2 id="requests-heading">统一任务</h2><div className="gateway-filters"><label>任务状态<select value={filter} onChange={e => { setFilter(e.target.value); setPage(1); setSelected(null) }}><option value="">全部状态</option>{Object.entries(labels).map(([key, label]) => <option value={key} key={key}>{label}</option>)}</select></label><label>服务编号筛选<input value={serviceFilter} onChange={e => { setServiceFilter(e.target.value); setPage(1); setSelected(null) }} /></label></div>
        <div className="gateway-scroll"><table><thead><tr><th>任务 / 应用</th><th>服务</th><th>状态 / 等待原因</th><th>受理时间</th></tr></thead><tbody>{items.map(item => <tr key={item.id}><td><button type="button" onClick={() => setSelected(item)} aria-label={`查看任务 ${item.id}`}>{item.id.slice(0, 12)}…</button><small>{item.applicationId}</small></td><td>{item.serviceId}<small>{item.kind.toUpperCase()}</small></td><td>{labels[item.status] || item.status}<small>{item.reason || '—'}</small></td><td>{time(item.acceptedAt)}</td></tr>)}</tbody></table></div>
        {!items.length && <p>当前筛选下没有任务。</p>}<div className="gateway-filters"><button type="button" disabled={page <= 1 || busy} onClick={() => setPage(p => p - 1)}>上一页</button><span>第 {page} 页，共 {total} 项</span><button type="button" disabled={page * 20 >= total || busy} onClick={() => setPage(p => p + 1)}>下一页</button></div>
        {selected && <article className="gateway-detail"><h3>任务 {selected.id}</h3><p>平台状态：{labels[selected.status] || selected.status}；容量：{selected.permitState}</p>{monitor && <dl><dt>平台已分配</dt><dd>{device(monitor.assignedDevice)}</dd><dt>服务实际执行设备</dt><dd>{monitor.monitoring?.actualDevice ? `${device(monitor.monitoring.actualDevice)}（${monitor.monitoring.actualDevice.namespace === 'host' ? '宿主机' : '服务内编号'}）` : '未提供'}</dd><dt>处理进度</dt><dd>{typeof monitor.monitoring?.progress === 'number' ? `${monitor.monitoring.progress}%` : monitor.monitoring?.supported ? '服务暂未报告进度' : '服务未提供进度'}</dd><dt>业务内部排队位置</dt><dd>{monitor.monitoring?.queuePosition ?? '未知'}</dd><dt>服务阶段 / 状态</dt><dd>{monitor.monitoring?.stage || '未提供'} / {monitor.monitoring?.remoteState || '未知'}</dd><dt>最近可信更新时间</dt><dd>{time(monitor.monitoring?.lastTrustedAt)}</dd><dt>查询名额</dt><dd>{monitor.monitoring?.queryBudgetReason ? budgetReasons[monitor.monitoring.queryBudgetReason] || "查询许可待核对" : monitor.monitoring?.queryBudgetState === "querying" ? "已取得名额，正在查询" : "无查询等待记录"}</dd><dt>监控异常</dt><dd>{monitor.nodeQueryError || monitor.executionError || monitor.monitoring?.lastError || '无'}</dd><dt>结果</dt><dd>{monitor.resultSaved ? '平台已保存' : monitor.resultPending ? '服务已完成，结果待获取' : '尚未保存'}</dd></dl>}
          <h4>任务时间线</h4><p>{events?.historyNotice}</p><ol className="gateway-timeline">{events?.items.map(event => <li key={event.sequence}><time>{time(event.occurredAt)}</time> <strong>{event.eventType}</strong> <small>{event.source === 'service' ? '服务报告' : '平台记录'}</small><pre>{JSON.stringify(event.details, null, 2)}</pre></li>)}</ol>{events?.hasMore && <button type="button" disabled={busy} onClick={() => void act(async () => { const next = await gatewayApi.events(selected.invocationId, events.nextSequence); setEvents({ ...next, items: [...events.items, ...next.items] }) })}>加载更多事件</button>}
        </article>}
      </section>
      <section className="gateway-card"><h2>Node 节点</h2><p>页面读取平台缓存；显式刷新只读取 Node 的就绪档案，不执行模型。</p><button type="button" disabled={busy} onClick={() => void act(async () => setNodes((await gatewayApi.refreshNodes()).items))}>核对节点状态</button><div className="gateway-scroll"><table><thead><tr><th>节点 / 本机服务</th><th>中心服务 / 目标</th><th>接单状态</th><th>上次核对 / 接入问题</th></tr></thead><tbody>{nodes.map(node => <tr key={`${node.nodeId}-${node.nodeServiceId}-${node.targetId}`}><td>{node.nodeId}<small>{node.nodeServiceId}</small></td><td>{node.serviceId}<small>{node.targetId}</small></td><td>{node.stale ? '观测已过期或未核对' : node.snapshot?.acceptingRequests && node.snapshot.adapterConfigured ? '可接单' : '不可接单'}</td><td>{time(node.checkedAt)}<small>{node.snapshot?.errorCode || node.snapshot?.issues?.join(', ') || '无已记录问题'}</small></td></tr>)}</tbody></table></div></section>
      <section className="gateway-card"><h2>并发额度</h2><p>{capacity?.notice}</p><div className="gateway-scroll"><table><thead><tr><th>资源</th><th>额度</th><th>已占用</th><th>未知占位</th></tr></thead><tbody>{capacity?.resources.map(row => <tr key={row.resourceKey}><td>{row.resourceKey}</td><td>{row.concurrencyLimit}</td><td>{row.held}</td><td>{row.uncertain}</td></tr>)}</tbody></table></div></section>
    </>}
  </section>
}

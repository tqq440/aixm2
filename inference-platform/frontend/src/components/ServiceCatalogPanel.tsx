import { useEffect, useRef, useState } from 'react'
import { asApiError } from '@/api/client'
import { serviceCatalogApi as api } from '@/api/service-catalog'
import type { CatalogConfig, CatalogEntry, CatalogGroup, CatalogGroupView, CatalogPage, CatalogTarget, ServiceKind, ServiceProtocol } from '@/types/serviceCatalog'
import '@/service-catalog.css'

const defaults = {
  asr: { protocol: 'remote-v2', invokePath: '/api/v1/asr/transcripts', healthPath: '/api/v1/asr/health' },
  vision: { protocol: 'vision-json-v1', invokePath: '/v1/vision/detections', healthPath: '/health' },
  llm: { protocol: 'chat-completions-sse', invokePath: '/v1/chat/completions', healthPath: '/v1/models' },
} as const
const target = (kind: ServiceKind, id = 'primary'): CatalogTarget => ({
  targetId: id, name: '主要目标', enabled: true, baseUrl: '', invokePath: defaults[kind].invokePath,
  healthPath: defaults[kind].healthPath, upstreamModel: null,
  apiKeyEnv: null, caFile: null, insecureTlsOrigin: null, connectTimeoutSeconds: 5, totalTimeoutSeconds: kind === 'llm' ? 120 : 600,
  concurrencyLimit: 1, capacityEvidence: null, resourceGroupIds: [], deviceKind: 'unknown', deviceId: null, deviceEvidence: null, compatibilityEvidence: null,
  monitoring: { protocol: 'none', statusPath: null, resultPath: null, reportsProgress: false, reportsDevice: false, pollSeconds: 5 },
})
export const emptyCatalogConfig = (): CatalogConfig => ({
  serviceId: '', name: '', kind: 'asr', modelId: '', modelVersion: null, protocol: 'remote-v2', invocationMode: 'sync',
  policy: 'fixed', maxFileBytes: 20 * 1024 * 1024, maxAudioSeconds: 300, targets: [target('asr')],
})
const emptyGroup = (): CatalogGroup => ({ groupId: '', name: '', concurrencyLimit: 1, capacityEvidence: null })

export function ServiceCatalogPanel() {
  const [page, setPage] = useState<CatalogPage | null>(null)
  const [pageNumber, setPageNumber] = useState(1)
  const [groups, setGroups] = useState<CatalogGroupView[]>([])
  const [groupPage, setGroupPage] = useState(1)
  const [groupTotal, setGroupTotal] = useState(0)
  const [group, setGroup] = useState(emptyGroup)
  const [groupVersion, setGroupVersion] = useState<number | null>(null)
  const [entry, setEntry] = useState<CatalogEntry | null>(null)
  const [draft, setDraft] = useState(emptyCatalogConfig)
  const [busy, setBusy] = useState(false)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const alive = useRef(true)
  const generation = useRef(0)
  const dirty = !entry || JSON.stringify(draft) !== JSON.stringify(entry.config)

  const load = async (number = pageNumber, groupNumber = groupPage) => {
    const token = ++generation.current
    setLoading(true)
    const results = await Promise.allSettled([api.list(number), api.groups(groupNumber)])
    if (!alive.current || token !== generation.current) return
    if (results[0].status === 'fulfilled' && results[1].status === 'fulfilled') {
      setPage(results[0].value); setGroups(results[1].value.items); setGroupTotal(results[1].value.total); setError(null)
    } else {
      const failure = results.find(result => result.status === 'rejected') as PromiseRejectedResult
      setError(asApiError(failure.reason).message)
    }
    setLoading(false)
  }
  useEffect(() => { alive.current = true; void load(pageNumber, groupPage); return () => { alive.current = false; generation.current++ } }, [pageNumber, groupPage]) // Reads only.
  const choose = (value: CatalogEntry | null) => { setEntry(value); setDraft(value ? structuredClone(value.config) : emptyCatalogConfig()); setNotice(null) }
  const act = async (operation: () => Promise<string | void>) => {
    setBusy(true); setNotice(null); setError(null)
    try { const message = await operation(); if (alive.current) { if (message) setNotice(message); await load() } }
    catch (failure) { if (alive.current) setError(asApiError(failure).message) }
    finally { if (alive.current) setBusy(false) }
  }
  const update = <K extends keyof CatalogConfig>(key: K, value: CatalogConfig[K]) => setDraft(previous => ({ ...previous, [key]: value }))
  const updateTarget = <K extends keyof CatalogTarget>(index: number, key: K, value: CatalogTarget[K]) =>
    setDraft(previous => ({ ...previous, targets: previous.targets.map((item, i) => i === index ? { ...item, [key]: value } : item) }))
  const changeKind = (kind: ServiceKind) => {
    setDraft(previous => ({ ...previous, kind, protocol: defaults[kind].protocol,
      invocationMode: kind === 'llm' ? 'stream' : 'sync', maxFileBytes: (kind === 'vision' ? 10 : 20) * 1024 * 1024,
      targets: previous.targets.map(item => ({ ...item, invokePath: defaults[kind].invokePath, healthPath: defaults[kind].healthPath,
        totalTimeoutSeconds: kind === 'llm' ? 120 : 600 })) }))
  }
  const save = () => act(async () => {
    const value = await api.save(draft, entry?.version ?? null)
    if (alive.current) { setEntry(value); setDraft(value.config) }
    return '草稿已保存。目录启用需要连接测试；当前任务仍使用原执行链路。'
  })

  return <section className="service-catalog" aria-labelledby="service-catalog-title">
    <div className="page-heading"><div><div className="page-eyebrow">SERVICE CATALOG · 模块 1</div><h1 id="service-catalog-title">公共服务目录</h1><p>统一维护 ASR、YOLO、LLM 的接口、资源资料与监控能力。</p></div>
      <button className="button button-secondary" disabled={busy || loading} onClick={() => void load()}>刷新目录</button></div>
    <p className="catalog-boundary" role="note">公共服务通过关联的 Node 执行，就绪状态与额度须另行核实。保存、测试或目录启用都不会切换现有任务、立即发送推理请求或启停模型。</p>
    {error && <div className="catalog-error" role="alert">{error}</div>}
    {notice && <p className="catalog-notice" role="status">{notice}</p>}
    <div className="catalog-grid">
      <section className="panel catalog-list" aria-label="已登记服务">
        <h2>服务列表 <small>{page?.total ?? 0}</small></h2>
        <div className="catalog-actions"><button className="button button-primary" disabled={busy} onClick={() => choose(null)}>添加服务</button>
          <button className="button button-secondary" disabled={busy || loading} onClick={() => void act(async () => {
            const result = await api.importAsr(); return `导入 ${result.imported.length} 项，跳过 ${result.skipped.length} 项。${result.message}`
          })}>导入已有 ASR 配置</button></div>
        {loading && !page && <p role="status">正在读取目录…</p>}
        {!loading && page?.items.length === 0 && <p>暂无服务。可以添加配置或显式导入已有 ASR。</p>}
        {page?.items.map(item => <button key={item.serviceId} className={`catalog-item ${entry?.serviceId === item.serviceId ? 'selected' : ''}`} disabled={busy} onClick={() => choose(item)}>
          <strong>{item.config.name}</strong><span>{item.config.kind.toUpperCase()} · {item.serviceId}</span>
          <small>{item.enabled ? '目录已启用' : '未启用'} · 执行资格以 Node 状态为准{item.activeRevision && item.activeRevision !== item.draftRevision ? ' · 有新草稿' : ''}</small>
        </button>)}
        <div className="catalog-actions"><button className="button button-quiet" disabled={busy || loading || pageNumber <= 1} onClick={() => setPageNumber(value => value - 1)}>上一页</button>
          <span>第 {pageNumber} 页</span><button className="button button-quiet" disabled={busy || loading || !page || pageNumber * page.pageSize >= page.total} onClick={() => setPageNumber(value => value + 1)}>下一页</button></div>
      </section>
      <section className="panel catalog-editor" aria-labelledby="catalog-editor-title">
        <h2 id="catalog-editor-title">{entry ? '编辑服务草稿' : '添加服务'}</h2>
        <form onSubmit={event => { event.preventDefault(); void save() }}>
          <fieldset disabled={busy || loading} className="catalog-fields">
            <label>服务编号<input required value={draft.serviceId} disabled={entry !== null} pattern="[A-Za-z0-9][A-Za-z0-9._-]*" maxLength={64} onChange={event => update('serviceId', event.target.value)} /></label>
            <label>服务名称<input required value={draft.name} maxLength={100} onChange={event => update('name', event.target.value)} /></label>
            <label>服务类型<select value={draft.kind} onChange={event => changeKind(event.target.value as ServiceKind)}><option value="asr">ASR 音频转写</option><option value="vision">YOLO 图片检测</option><option value="llm">LLM 文本生成</option></select></label>
            <label>接口协议<select value={draft.protocol} onChange={event => {
              const protocol = event.target.value as ServiceProtocol; update('protocol', protocol)
              if (draft.kind === 'asr') setDraft(previous => ({ ...previous, protocol, targets: previous.targets.map(item => ({ ...item,
                invokePath: protocol === 'openai-audio' ? '/v1/audio/transcriptions' : defaults.asr.invokePath,
                healthPath: protocol === 'openai-audio' ? '/v1/models' : defaults.asr.healthPath })) }))
            }}>{(draft.kind === 'asr' ? ['remote-v2', 'openai-audio'] : [defaults[draft.kind].protocol]).map(value => <option key={value}>{value}</option>)}</select></label>
            <label>平台模型编号<input required value={draft.modelId} maxLength={64} onChange={event => update('modelId', event.target.value)} /></label>
            <label>上游版本（可选）<input value={draft.modelVersion ?? ''} maxLength={64} onChange={event => update('modelVersion', event.target.value || null)} /></label>
            <label>调用模式<select value={draft.invocationMode} onChange={event => update('invocationMode', event.target.value as CatalogConfig['invocationMode'])} disabled={draft.kind === 'llm'}>
              {draft.kind === 'llm' ? <option value="stream">实时流式</option> : <><option value="sync">同步返回结果</option><option value="async">异步返回上游任务编号</option></>}
            </select></label>
            <label>调度策略<select value={draft.policy} onChange={event => update('policy', event.target.value as CatalogConfig['policy'])}><option value="fixed">固定目标</option><option value="gpu_preferred">GPU 优先（需设备及兼容依据）</option></select></label>
            {draft.kind !== 'llm' && <label>文件上限（MiB）<input type="number" min={1} max={draft.kind === 'vision' ? 10 : 20} value={draft.maxFileBytes / 1024 / 1024} onChange={event => update('maxFileBytes', Number(event.target.value) * 1024 * 1024)} /></label>}
            {draft.kind === 'asr' && <label>音频上限（秒）<input type="number" min={1} max={300} value={draft.maxAudioSeconds} onChange={event => update('maxAudioSeconds', Number(event.target.value))} /></label>}
          </fieldset>
          {draft.targets.map((item, index) => <fieldset disabled={busy || loading} key={index} className="catalog-target">
            <legend>调用目标 {index + 1}</legend>
            <div className="catalog-fields">
              <label>目标编号<input required value={item.targetId} maxLength={64} onChange={event => updateTarget(index, 'targetId', event.target.value)} /></label>
              <label>目标名称<input required value={item.name} onChange={event => updateTarget(index, 'name', event.target.value)} /></label>
              <label className="catalog-wide">服务地址<input required type="url" placeholder="http://服务器地址:端口" value={item.baseUrl} onChange={event => updateTarget(index, 'baseUrl', event.target.value)} /></label>
              <label>调用路径<input required value={item.invokePath} onChange={event => updateTarget(index, 'invokePath', event.target.value)} /></label>
              <label>健康路径<input required value={item.healthPath} onChange={event => updateTarget(index, 'healthPath', event.target.value)} /></label>
              <label>上游模型名<input required={draft.protocol !== 'remote-v2'} value={item.upstreamModel ?? ''} onChange={event => updateTarget(index, 'upstreamModel', event.target.value || null)} /></label>
              <label>目标并发额度<input type="number" min={1} max={64} value={item.concurrencyLimit} onChange={event => updateTarget(index, 'concurrencyLimit', Number(event.target.value))} /></label>
              <label>目标容量依据<input value={item.capacityEvidence ?? ''} onChange={event => updateTarget(index, 'capacityEvidence', event.target.value || null)} /></label>
              <label>目标设备资料<select value={item.deviceKind} onChange={event => {
                const deviceKind = event.target.value as CatalogTarget['deviceKind']; updateTarget(index, 'deviceKind', deviceKind)
                if (deviceKind === 'unknown') setDraft(previous => ({ ...previous, targets: previous.targets.map((value, i) => i === index ? { ...value, deviceKind, deviceId: null, deviceEvidence: null } : value) }))
              }}><option value="unknown">未知</option><option value="cpu">CPU</option><option value="gpu">GPU</option></select></label>
              <label>设备标识<input value={item.deviceId ?? ''} disabled={item.deviceKind === 'unknown'} placeholder="主机 / GPU 0" onChange={event => updateTarget(index, 'deviceId', event.target.value || null)} /></label>
              <label>设备核实依据<input value={item.deviceEvidence ?? ''} disabled={item.deviceKind === 'unknown'} onChange={event => updateTarget(index, 'deviceEvidence', event.target.value || null)} /></label>
              <label>模型兼容依据<input value={item.compatibilityEvidence ?? ''} onChange={event => updateTarget(index, 'compatibilityEvidence', event.target.value || null)} /></label>
              <label className="catalog-wide">共享资源组编号（逗号分隔）<input value={item.resourceGroupIds.join(',')} placeholder="先在下方登记，例如 lab-gpu0,asr-pool" onChange={event => updateTarget(index, 'resourceGroupIds', event.target.value.split(',').map(value => value.trim()).filter(Boolean))} /></label>
              <label>监控能力<select value={item.monitoring.protocol} onChange={event => updateTarget(index, 'monitoring', event.target.value === 'none'
                ? { protocol: 'none', statusPath: null, resultPath: null, reportsProgress: false, reportsDevice: false, pollSeconds: 5 }
                : { ...item.monitoring, protocol: 'job-json-v1', statusPath: '/jobs/{requestId}', resultPath: '/jobs/{requestId}/result' })}>
                <option value="none">服务未提供查询接口</option><option value="job-json-v1">逐任务 JSON 查询（由 Node 后台采集）</option></select></label>
              {item.monitoring.protocol !== 'none' && <>
                <label>状态查询路径<input required value={item.monitoring.statusPath ?? ''} onChange={event => updateTarget(index, 'monitoring', { ...item.monitoring, statusPath: event.target.value })} /></label>
                <label>结果查询路径<input value={item.monitoring.resultPath ?? ''} onChange={event => updateTarget(index, 'monitoring', { ...item.monitoring, resultPath: event.target.value || null })} /></label>
                <label className="catalog-checkbox"><input type="checkbox" checked={Boolean(item.monitoring.resultInStatus)} onChange={event => updateTarget(index, 'monitoring', { ...item.monitoring, resultInStatus: event.target.checked })} />状态接口直接提供结果</label>
                <label>查询间隔（秒）<input type="number" min={5} max={60} value={item.monitoring.pollSeconds} onChange={event => updateTarget(index, 'monitoring', { ...item.monitoring, pollSeconds: Number(event.target.value) })} /></label>
                <label className="catalog-checkbox"><input type="checkbox" checked={item.monitoring.reportsProgress} onChange={event => updateTarget(index, 'monitoring', { ...item.monitoring, reportsProgress: event.target.checked })} />服务声明提供进度</label>
                <label className="catalog-checkbox"><input type="checkbox" checked={item.monitoring.reportsDevice} onChange={event => updateTarget(index, 'monitoring', { ...item.monitoring, reportsDevice: event.target.checked })} />服务声明提供实际设备</label>
              </>}
            </div>
            <details><summary>关联目标 Node</summary><p>填写已登记的 Node 服务与不可变修订；这里只配置关联，不启动业务。</p><label className="catalog-checkbox"><input type="checkbox" checked={Boolean(item.node)} onChange={event => updateTarget(index, 'node', event.target.checked ? { nodeId: '', serviceId: '', configRevision: '', baseUrl: '', executorTokenEnv: '' } : null)} />通过 Node 执行</label>{item.node && <div className="catalog-fields">{(['nodeId', 'serviceId', 'configRevision', 'baseUrl', 'executorTokenEnv'] as const).map((key, i) => <label key={key}>{['Node 编号', 'Node 本机服务编号', 'Node 配置修订（64位）', 'Node 地址', '中心执行凭据环境变量名'][i]}<input required value={item.node![key]} autoComplete="off" onChange={event => updateTarget(index, 'node', { ...item.node!, [key]: event.target.value })} /></label>)}</div>}</details>
            <details><summary>认证、证书与超时</summary><div className="catalog-fields">
              {(['apiKeyEnv', 'caFile', 'insecureTlsOrigin'] as const).map((key, i) => <label key={key}>{['凭据环境变量名', '可信 CA 路径', '准确 HTTPS Origin 例外'][i]}<input value={item[key] ?? ''} autoComplete="off" onChange={event => updateTarget(index, key, event.target.value || null)} /></label>)}
              <label>连接超时（秒）<input type="number" min={0.1} max={30} step={0.1} value={item.connectTimeoutSeconds} onChange={event => updateTarget(index, 'connectTimeoutSeconds', Number(event.target.value))} /></label>
              <label>总超时（秒）<input type="number" min={1} max={900} value={item.totalTimeoutSeconds} onChange={event => updateTarget(index, 'totalTimeoutSeconds', Number(event.target.value))} /></label>
            </div></details>
            <div className="catalog-actions"><label className="catalog-checkbox"><input type="checkbox" checked={item.enabled} onChange={event => updateTarget(index, 'enabled', event.target.checked)} />纳入目录启用检查</label>
              <button type="button" className="button button-secondary" disabled={dirty || !entry} onClick={() => entry && void act(async () => {
                const result = await api.test(entry, item.targetId); const fresh = await api.get(entry.serviceId)
                if (alive.current) setEntry(fresh); return result.message + (result.errorCode ? `（${result.errorCode}）` : '')
              })}>测试此目标连接</button>
              <button type="button" className="button button-quiet" disabled={draft.targets.length === 1} onClick={() => update('targets', draft.targets.filter((_, i) => i !== index))}>移除此目标草稿</button></div>
          </fieldset>)}
          <div className="catalog-actions"><button className="button button-primary" type="submit" disabled={busy || loading}>保存草稿</button>
            <button className="button button-secondary" type="button" disabled={busy || loading || draft.targets.length >= 8} onClick={() => update('targets', [...draft.targets, target(draft.kind, `target-${draft.targets.length + 1}`)])}>添加目标</button>
            <button className="button button-secondary" type="button" disabled={busy || loading || dirty || !entry} onClick={() => entry && void act(async () => {
              const value = await api.activate(entry, !entry.enabled || entry.activeRevision !== entry.draftRevision); if (alive.current) setEntry(value)
              return value.enabled ? '目录已启用；新请求采用此修订，在途任务不变。' : '目录已停用；在途任务不受影响。'
            })}>{entry?.enabled && entry.activeRevision === entry.draftRevision ? '停用目录服务' : '启用目录修订'}</button></div>
        </form>
        {entry && <div className="catalog-status"><p>草稿修订：<code>{entry.draftRevision}</code></p><p>已选目录修订：<code>{entry.activeRevision ?? '未选择'}</code></p>
          <p>上游版本：{entry.config.modelVersion ?? '未知；目录修订只标识连接配置，不证明权重固定'}</p>
          {entry.checks.map(check => <p key={`${check.revision}:${check.targetId}`}>{check.targetId}：{check.success ? '连接检查通过' : '连接检查失败'} · {check.durationMs} ms · {check.checkedAt} · {check.errorCode}</p>)}
          <h3>接入问题与能力声明</h3><ul>{entry.issues.map(issue => <li key={`${issue.code}:${issue.targetId}`}><strong>{issue.blocksActivation ? '需处理' : '说明'}</strong> {issue.targetId && `${issue.targetId}：`}{issue.message}</li>)}</ul>
        </div>}
      </section>
    </div>
    <section className="panel catalog-groups" aria-labelledby="catalog-groups-title"><h2 id="catalog-groups-title">共享资源组资料</h2><p>同一物理 GPU 或同一服务池应引用相同组编号。公共执行统一预约共享额度；编辑资料不会新增真实算力。</p>
      <div className="catalog-actions">{groups.map(item => <button key={item.groupId} className="button button-secondary" disabled={busy} onClick={() => {
        setGroup({ groupId: item.groupId, name: item.name, concurrencyLimit: item.concurrencyLimit, capacityEvidence: item.capacityEvidence }); setGroupVersion(item.version)
      }}>{item.name} · {item.groupId} · 额度 {item.concurrencyLimit}</button>)}
        <button className="button button-quiet" disabled={busy} onClick={() => { setGroup(emptyGroup()); setGroupVersion(null) }}>新建资源组</button>
        <button className="button button-quiet" disabled={busy || loading || groupPage <= 1} onClick={() => setGroupPage(value => value - 1)}>前一组页</button>
        <button className="button button-quiet" disabled={busy || loading || groupPage * 100 >= groupTotal} onClick={() => setGroupPage(value => value + 1)}>后一组页</button></div>
      <form onSubmit={event => { event.preventDefault(); void act(async () => { await api.saveGroup(group, groupVersion); setGroup(emptyGroup()); setGroupVersion(null); return '资源组资料已保存，未改变日常容量许可。' }) }}>
        <fieldset disabled={busy || loading} className="catalog-fields">
          <label>资源组编号<input required value={group.groupId} disabled={groupVersion !== null} onChange={event => setGroup({ ...group, groupId: event.target.value })} /></label>
          <label>资源组名称<input required value={group.name} onChange={event => setGroup({ ...group, name: event.target.value })} /></label>
          <label>资源组并发额度<input required type="number" min={1} max={64} value={group.concurrencyLimit} onChange={event => setGroup({ ...group, concurrencyLimit: Number(event.target.value) })} /></label>
          <label>容量依据<input value={group.capacityEvidence ?? ''} onChange={event => setGroup({ ...group, capacityEvidence: event.target.value || null })} /></label>
          <button className="button button-secondary" type="submit">保存资源组</button>
        </fieldset>
      </form>
    </section>
  </section>
}

import { useState } from 'react'
import type { FormEvent } from 'react'
import type { useLlm } from '../hooks/useLlm'
import { onlineFinished } from '../types/llm'
import type { OnlineState } from '../types/llm'
import { dateTime, downloadText } from '../utils/format'
import { ErrorNotice } from './Feedback'
import { Icon } from './Icon'

type Platform = ReturnType<typeof useLlm>
const labels: Record<OnlineState, string> = { accepted: '已受理', waiting_instance: '等待服务', streaming: '生成中', completed: '已完成', failed: '失败', cancelled: '已取消' }
const permitLabels = { waiting: '等待准入', held: '占用中', released: '已释放', uncertain: '释放未确认' }

export function LlmWorkspace({ platform }: { platform: Platform }) {
  const [prompt, setPrompt] = useState('')
  const [modelKey, setModelKey] = useState('')
  const [parameters, setParameters] = useState<{ temperature: string; maxTokens: string } | null>(null)
  const [feedback, setFeedback] = useState('')
  const model = platform.models.find(item => `${item.modelId}:${item.modelVersion}` === modelKey) ?? platform.models[0]
  const values = parameters ?? { temperature: String(model?.parameterSchema.defaultTemperature ?? 0.7), maxTokens: String(model?.parameterSchema.defaultMaxTokens ?? 512) }
  const temperature = Number(values.temperature), maxTokens = Number(values.maxTokens)
  const promptLength = Array.from(prompt).length
  const invalid = !prompt.trim() ? null : model && promptLength > model.inputConstraints.maxInputCharacters ? '输入文字超过模型允许的字符数。'
    : !Number.isFinite(temperature) || values.temperature.trim() === '' || temperature < 0 || temperature > 2 ? '温度必须在 0 到 2 之间。'
      : !Number.isInteger(maxTokens) || maxTokens < 1 || (model && maxTokens > model.parameterSchema.maxTokens) ? `最多输出 token 数必须为 1 到 ${model?.parameterSchema.maxTokens ?? 2048} 的整数。` : null
  const record = platform.record?.onlineRequestId === platform.selectedId ? platform.record : null
  const occupied = Boolean(record && !onlineFinished(record))
  const canGenerate = Boolean(platform.status?.configured && model && !platform.statusError && !platform.modelsError && !platform.running && !platform.cancelling && !occupied && prompt.trim() && !invalid)
  const submit = (event: FormEvent) => {
    event.preventDefault()
    if (!canGenerate || !model) return
    setFeedback('')
    void platform.start({ modelId: model.modelId, modelVersion: model.modelVersion, messages: [{ role: 'user', content: prompt }], parameters: { temperature, maxTokens } })
  }
  const copy = async () => {
    if (!platform.output?.text) return
    try { await navigator.clipboard.writeText(platform.output.text); setFeedback('已复制当前收到的文字') }
    catch { setFeedback('浏览器未允许复制，请选中文字后手动复制。') }
  }
  const save = () => {
    if (!platform.output?.text) return
    const incomplete = !platform.output.complete
    downloadText(`llm-${platform.output.id.slice(0, 8)}${incomplete ? '-partial' : ''}.txt`, platform.output.text, 'text/plain;charset=utf-8')
    setFeedback(incomplete ? '已下载当前收到的部分文字，文件名标记为 partial' : '已发起文字下载')
  }

  return <div data-workspace="llm">
    <section className="panel llm-service-panel" aria-label="大模型服务状态">
      <div className="llm-service-icon"><Icon name="server" size={23} /></div>
      <div className="llm-service-copy"><h2>{platform.statusError ? '无法读取服务状态' : !platform.status ? '正在读取服务状态…' : platform.status.configured ? '已配置在线服务 · 实际可用性待验证' : '尚未配置大模型服务'}</h2><p>{platform.status?.message ?? '页面根据后台配置显示可用模型，不提供模拟回答。'}</p>
        {platform.status && <p className="llm-service-metrics">在线请求上限 {platform.status.maxConcurrentRequests} · 当前请求 {platform.status.activeRequests} · 释放未确认 {platform.status.uncertainPermits}</p>}</div>
      <span className={`llm-service-tag ${platform.status?.configured ? 'is-configured' : ''}`}>{platform.status?.configured ? '未做真实模型验收' : '不可生成'}</span>
    </section>
    <ErrorNotice error={platform.statusError} title="服务状态读取失败" />
    <div className="llm-work-grid">
      <section className="panel llm-compose-panel" aria-labelledby="llm-compose-title">
        <div className="panel-heading"><div><span className="section-kicker">ONLINE GENERATION</span><h2 id="llm-compose-title">新建文本请求</h2></div><span className="step-number">01</span></div>
        <p className="panel-intro">输入文字，通过已配置的服务逐步接收回答。</p>
        <form onSubmit={submit}>
          <label className="field-label" htmlFor="llm-model">大模型</label>
          <select id="llm-model" value={model ? `${model.modelId}:${model.modelVersion}` : ''} disabled={platform.running || !model} onChange={event => { setModelKey(event.target.value); setParameters(null) }}>
            {!model && <option value="">暂无可用的在线模型</option>}{platform.models.map(item => <option key={`${item.modelId}:${item.modelVersion}`} value={`${item.modelId}:${item.modelVersion}`}>{item.modelId}</option>)}
          </select>
          {model && <p className="field-hint model-version">模型版本：<code>{model.modelVersion}</code></p>}
          <ErrorNotice error={platform.modelsError} title="模型目录读取失败" />
          <label className="field-label" htmlFor="llm-prompt">输入内容</label>
          <textarea id="llm-prompt" rows={8} value={prompt} disabled={platform.running} placeholder="在此输入问题或需要处理的文字" onChange={event => setPrompt(event.target.value)} aria-describedby="llm-input-limit" />
          <p className="llm-character-count" id="llm-input-limit">{promptLength.toLocaleString('zh-CN')} {model ? `/ ${model.inputConstraints.maxInputCharacters.toLocaleString('zh-CN')}` : ''} 字符 · 单次文本请求</p>
          <div className="llm-parameters"><div><label className="field-label" htmlFor="llm-temperature">温度</label><input id="llm-temperature" type="number" min="0" max="2" step="any" required disabled={!model || platform.running} value={values.temperature} onChange={event => setParameters({ ...values, temperature: event.target.value })} /><p>较低时输出更稳定。</p></div><div><label className="field-label" htmlFor="llm-max-tokens">最多输出 token</label><input id="llm-max-tokens" type="number" min="1" max={model?.parameterSchema.maxTokens ?? 2048} step="1" required disabled={!model || platform.running} value={values.maxTokens} onChange={event => setParameters({ ...values, maxTokens: event.target.value })} /><p>token 数不等于汉字数。</p></div></div>
          <ErrorNotice error={invalid ? { message: invalid } : null} />
          {!platform.status?.configured && !platform.statusError && <p className="llm-unconfigured-note">当前未连接大模型服务，开始生成已禁用。配置真实服务后刷新状态；此页面不会生成示例回答。</p>}
          {occupied && !platform.running && <p className="llm-unconfigured-note">当前请求尚未结束。请核对状态或请求停止，再开始新的生成。</p>}
          <button className="button button-primary submit-button" type="submit" disabled={!canGenerate}><span>{platform.running ? '正在接收生成…' : '开始生成'}</span><Icon name={platform.running ? 'refresh' : 'arrow'} className={platform.running ? 'spin' : undefined} size={18} /></button>
        </form>
        <div className="privacy-note"><span className="tiny-dot" /><p>输入与回复正文不写入平台历史记录。刷新后只恢复请求状态；在线生成依赖当前连接，离开或隐藏页面会尝试停止。</p></div>
      </section>
      <div className="results-column">
        <section className="panel llm-output-panel" aria-label="在线生成结果" aria-busy={platform.running}>
          <div className="panel-heading"><div><span className="section-kicker">STREAMING RESPONSE</span><h2>在线生成结果</h2></div>{record && <span className={`status-badge status-${record.state === 'completed' ? 'succeeded' : record.state === 'streaming' ? 'running' : record.state}`}><span />{labels[record.state]}</span>}</div>
          {!platform.selectedId && <div className="empty-state detail-empty"><span className="empty-icon"><Icon name="chat" size={32} /></span><h3>{platform.status?.configured ? '等待一次真实请求' : '等待配置真实大模型服务'}</h3><p>这里只展示服务实际返回的文字，不预置回答。</p></div>}
          {platform.selectedId && <>
            <div className="task-reference llm-reference"><span>请求编号</span><code>{platform.selectedId}</code></div>
            <ErrorNotice error={platform.recordError} title="请求状态读取失败" />
            <ErrorNotice error={platform.streamError} title="生成未正常完成" />
            {platform.running && <p className="llm-stream-status" role="status"><Icon name="refresh" className="spin" size={15} />{platform.output?.text ? '正在接收服务返回的文字…' : '已发出请求，等待服务响应…'}</p>}
            {platform.output?.interrupted && <p className="llm-partial-note">本次输出不完整或结束状态尚未确认。已收到的文字保留在下方，不会自动重连或重新生成。</p>}
            {platform.output?.text ? <div className="llm-response-text" data-testid="llm-response-text">{platform.output.text}</div>
              : !platform.running && !platform.output ? <p className="llm-empty-body">这里只恢复了请求状态。输入与回复正文未持久保存，刷新或重新打开链接不能恢复正文，也不会继续生成。</p>
                : !platform.running && <p className="llm-empty-body">本次没有收到可展示的文字。</p>}
            {platform.output?.text && <div className="result-actions"><button className="button button-quiet" type="button" onClick={() => void copy()}><Icon name="copy" size={16} />复制已接收文字</button><button className="button button-quiet" type="button" onClick={save}><Icon name="download" size={16} />{platform.output.complete ? '下载 TXT' : '下载部分 TXT'}</button></div>}
            <div role="status" className="action-feedback">{feedback}</div>
            {(platform.running || (record && !onlineFinished(record))) && <div className="cancel-task-area"><button type="button" className="button button-cancel" onClick={platform.stop} disabled={platform.cancelling || Boolean(record?.cancelRequested)}><Icon name="close" size={16} />{platform.cancelling ? '正在请求停止…' : record?.cancelRequested ? '停止请求已受理' : '停止生成'}</button></div>}
            {platform.stopNote && <p className="cancel-feedback" role="status">{platform.stopNote}</p>}
            <ErrorNotice error={platform.cancelError} title="停止请求未确认" />
            {platform.cancelError && <button type="button" className="text-button" onClick={platform.stop} disabled={platform.cancelling}>重试停止请求</button>}
            {record && <details className="task-limits llm-execution"><summary><strong>请求与执行信息</strong><span>查看详情</span></summary><dl className="execution-grid">
              <div><dt>模型</dt><dd>{record.modelId}</dd></div><div><dt>模型版本</dt><dd>{record.modelVersion}</dd></div><div><dt>受理时间</dt><dd>{dateTime(record.acceptedAt)}</dd></div><div><dt>首字时间</dt><dd>{dateTime(record.firstTokenAt)}</dd></div><div><dt>结束时间</dt><dd>{dateTime(record.endedAt)}</dd></div><div><dt>截止时间</dt><dd>{dateTime(record.deadlineAt)}</dd></div><div><dt>后台输出字符计数</dt><dd>{record.outputCharacters}</dd></div><div><dt>结束原因</dt><dd>{record.finishReason ?? '—'}</dd></div><div><dt>在线准入许可</dt><dd>{permitLabels[record.permitState]}</dd></div><div><dt>取消停止确认</dt><dd>{record.cancellationConfirmed ? '后台已确认' : record.cancelRequested ? '尚未确认' : '未请求取消'}</dd></div><div className="wide"><dt>服务实例</dt><dd className="monospace">{record.instanceId ?? '尚未关联'}</dd></div>
            </dl>{record.error && <p>{record.error.message}（{record.error.code}）</p>}<p>在线准入许可反映平台记录，不代表 GPU 使用率。取消受理与引擎停止确认分别记录。</p></details>}
            <button type="button" className="text-button" onClick={platform.refresh}>核对请求状态</button>
          </>}
        </section>
        <section className="panel llm-history-panel" aria-label="在线请求记录">
          <div className="panel-heading"><div><span className="section-kicker">REQUEST HISTORY</span><h2>在线请求记录</h2></div><span className="llm-history-count">{platform.list?.total ?? '—'} 条</span></div>
          <p className="llm-history-note">仅保存生命周期和字符计数，不保存输入与回复正文。</p>
          <ErrorNotice error={platform.listError} title="请求记录读取失败" />
          {!platform.listError && !platform.list?.items.length && <p className="llm-history-empty">{platform.list ? '暂无在线请求记录。' : '正在读取请求记录…'}</p>}
          <div className="llm-history-list">{platform.list?.items.map(item => <button type="button" key={item.onlineRequestId} className={`llm-history-item ${item.onlineRequestId === platform.selectedId ? 'is-selected' : ''}`} onClick={() => platform.select(item.onlineRequestId)} aria-label={`查看在线请求 ${item.onlineRequestId}`} aria-pressed={item.onlineRequestId === platform.selectedId}><span><strong>{item.modelId}</strong><code>{item.onlineRequestId.slice(0, 12)}</code></span><span><small>{labels[item.state]}</small><time>{dateTime(item.acceptedAt)}</time></span></button>)}</div>
          {platform.list && platform.list.total > 0 && <div className="llm-pagination"><span>第 {platform.page} / {Math.max(1, Math.ceil(platform.list.total / platform.list.pageSize))} 页</span><div><button type="button" className="page-button" disabled={platform.page <= 1 || platform.loading} onClick={() => platform.setPage(platform.page - 1)}>上一页</button><button type="button" className="page-button" disabled={platform.page * platform.list.pageSize >= platform.list.total || platform.loading} onClick={() => platform.setPage(platform.page + 1)}>下一页</button></div></div>}
        </section>
      </div>
    </div>
  </div>
}

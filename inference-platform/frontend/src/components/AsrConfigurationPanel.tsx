import { useEffect, useRef, useState } from 'react'
import { asrServicesApi } from '@/api/asr-services'
import { asApiError, type ApiError } from '@/api/client'
import type { useAsrServices } from '@/hooks/useAsrServices'
import type { AsrServiceConfig, AsrServiceTestResult, AsrServiceView } from '@/types/asrServices'
import { ErrorNotice } from './Feedback'
import { Icon } from './Icon'
import '@/asr-service.css'

export const emptyAsrConfig = (): AsrServiceConfig => ({
  serviceId: '', name: '', protocol: 'remote-v2', baseUrl: '',
  transcribePath: '/api/v1/asr/transcripts', healthPath: '/api/v1/asr/health',
  modelId: '', modelVersion: null, upstreamModel: null, apiKeyEnv: null,
  caFile: null, insecureTlsOrigin: null, connectTimeoutSeconds: 5, totalTimeoutSeconds: 600,
})

function editable(value: AsrServiceConfig): AsrServiceConfig {
  const { serviceId, name, protocol, baseUrl, transcribePath, healthPath, modelId, modelVersion,
    upstreamModel, apiKeyEnv, caFile, insecureTlsOrigin, connectTimeoutSeconds, totalTimeoutSeconds } = value
  return { serviceId, name, protocol, baseUrl, transcribePath, healthPath, modelId, modelVersion,
    upstreamModel, apiKeyEnv, caFile, insecureTlsOrigin, connectTimeoutSeconds, totalTimeoutSeconds }
}

export function AsrConfigurationPanel({ services, canTestAudio, onTestAudio }: {
  services: ReturnType<typeof useAsrServices>
  canTestAudio: boolean
  onTestAudio: () => void
}) {
  const { state, error, actionError, loading, pending, refresh, save, remove, select } = services
  const [draft, setDraft] = useState(emptyAsrConfig)
  const [original, setOriginal] = useState<AsrServiceView | null>(null)
  const [testResult, setTestResult] = useState<AsrServiceTestResult | null>(null)
  const [testError, setTestError] = useState<ApiError | null>(null)
  const [testing, setTesting] = useState(false)
  const [notice, setNotice] = useState<string | null>(null)
  const [deleteConfirm, setDeleteConfirm] = useState(false)
  const testGeneration = useRef(0)
  const alive = useRef(true)
  useEffect(() => { alive.current = true; return () => { alive.current = false; testGeneration.current += 1 } }, [])
  const busy = Boolean(pending || testing)
  const clean = original !== null && JSON.stringify(draft) === JSON.stringify(editable(original))
  const valid = Boolean(draft.serviceId.trim() && draft.name.trim() && draft.baseUrl.trim() && draft.transcribePath.trim()
    && draft.healthPath.trim() && draft.modelId.trim() && (draft.protocol !== 'openai-audio' || draft.upstreamModel?.trim())
    && draft.connectTimeoutSeconds > 0 && draft.totalTimeoutSeconds > 0)
  const clearTest = () => { testGeneration.current += 1; setTestResult(null); setTestError(null); setNotice(null); setDeleteConfirm(false) }
  const change = <K extends keyof AsrServiceConfig>(key: K, value: AsrServiceConfig[K]) => {
    clearTest(); setDraft(previous => ({ ...previous, [key]: value }))
  }
  const choose = (value: AsrServiceView | null) => {
    clearTest(); setOriginal(value); setDraft(value ? editable(value) : emptyAsrConfig())
  }
  const text = (key: keyof AsrServiceConfig, label: string, required = false, placeholder?: string) => <label>
    <span className="field-label">{label}{required ? ' *' : ''}</span>
    <input aria-label={label} value={String(draft[key] ?? '')} required={required} disabled={busy || (key === 'serviceId' && original !== null)}
      autoComplete="off" spellCheck={false} placeholder={placeholder}
      onChange={event => change(key, event.target.value || (required ? '' : null) as never)} />
  </label>
  const saveDraft = async () => {
    clearTest()
    const result = await save(draft, original?.revision ?? null)
    if (result && alive.current) {
      const item = result.items.find(value => value.serviceId === draft.serviceId)
      if (item) { setOriginal(item); setDraft(editable(item)) }
      setNotice('配置已保存。点击“选用此修订”后，重启平台才会用于新任务。')
    }
  }
  const testDraft = async () => {
    const token = ++testGeneration.current
    setTesting(true); setTestResult(null); setTestError(null); setNotice(null)
    try {
      const result = await asrServicesApi.test(draft)
      if (alive.current && token === testGeneration.current) setTestResult(result)
    } catch (failure) {
      if (alive.current && token === testGeneration.current) setTestError(asApiError(failure))
    } finally { if (alive.current) setTesting(false) }
  }
  const deleteDraft = async () => {
    if (!original) return
    const result = await remove(original.serviceId, original.revision)
    if (result && alive.current) { choose(null); setNotice('配置已删除。运行中的服务与已有任务保持原配置，直到平台重启。') }
  }
  const selectedName = state?.items.find(item => item.serviceId === state.selectedServiceId)?.name ?? state?.selectedServiceId
  const runningName = state?.items.find(item => item.serviceId === state.runningServiceId)?.name ?? state?.runningServiceId
  return <section className="panel asr-service-panel asr-configuration-panel" aria-labelledby="asr-configuration-title">
    <div className="asr-service-heading"><div><span className="section-kicker">ASR CONNECTIONS</span><h2 id="asr-configuration-title">ASR 服务配置</h2></div>
      <button className="button button-secondary" type="button" onClick={refresh} disabled={loading || busy}><Icon name="refresh" size={15} />刷新配置</button></div>
    <p className="asr-service-description">手动填写服务接口；运行在本机、局域网或服务器的服务使用同一套配置。保存、连接测试和选用互相独立。</p>
    <div className="asr-config-runtime" aria-live="polite">
      <p><strong>当前运行：</strong>{state ? runningName ?? (state.runningModel ? '原环境配置' : '尚无接口服务') : '正在核对'}
        {state?.runningModel && <span> · {state.runningModel.modelId}</span>}</p>
      <p><strong>下次启动：</strong>{state ? selectedName ?? '原环境配置' : '正在核对'}
        {state?.restartRequired && <span className="asr-config-pending">待重启生效</span>}</p>
      {state?.runningRevision && <p className="asr-service-version">运行修订：<code>{state.runningRevision}</code></p>}
      {state?.selectedRevision && <p className="asr-service-version">已选修订：<code>{state.selectedRevision}</code></p>}
      <p className="asr-service-note">选用新的修订后，先重启平台再提交新音频。已有任务和未确认提交不会改到其他接口。</p>
    </div>
    <ErrorNotice error={error} title="服务配置读取失败" />
    <div className="asr-config-selector">
      <label><span className="field-label">编辑已保存配置</span><select aria-label="编辑已保存配置" value={original?.serviceId ?? ''} disabled={busy} onChange={event => choose(state?.items.find(item => item.serviceId === event.target.value) ?? null)}>
        <option value="">新建服务配置</option>
        {state?.items.map(item => <option key={item.serviceId} value={item.serviceId}>{item.name} · {item.serviceId}</option>)}
      </select></label>
      {state?.legacyConfiguration && <button type="button" className="button button-quiet" disabled={busy} onClick={() => {
        clearTest(); setOriginal(null); setDraft(editable(state.legacyConfiguration!)); setNotice('已带入原环境参数，检查服务标识后手动保存。')
      }}>带入原环境配置</button>}
      {original && <button type="button" className="button button-quiet" disabled={busy} onClick={() => choose(null)}>新建配置</button>}
    </div>
    <form onSubmit={event => { event.preventDefault(); if (valid && !busy) void saveDraft() }}>
      <div className="asr-config-grid">
        {text('serviceId', '服务标识', true, '例如 speech-main')}
        {text('name', '服务名称', true, '例如实验室语音服务')}
        <label><span className="field-label">接口协议 *</span><select aria-label="接口协议" value={draft.protocol} disabled={busy} onChange={event => {
          clearTest()
          const protocol = event.target.value as AsrServiceConfig['protocol']
          setDraft(value => ({ ...value, protocol, transcribePath: protocol === 'remote-v2' ? '/api/v1/asr/transcripts' : '/v1/audio/transcriptions',
            healthPath: protocol === 'remote-v2' ? '/api/v1/asr/health' : '/v1/models' }))
        }}><option value="remote-v2">SenseVoice Remote-V2</option><option value="openai-audio">OpenAI 兼容音频接口</option></select></label>
        {text('baseUrl', '服务地址', true, 'http://127.0.0.1:8000')}
        {text('transcribePath', '转写接口路径', true)}
        {text('healthPath', '健康检查路径', true)}
        {text('modelId', '平台模型标识', true, '例如 sensevoice-main')}
        {text('modelVersion', '上游模型版本（可选）')}
        {draft.protocol === 'openai-audio' && text('upstreamModel', '接口 model 参数', true, '服务实际使用的模型名称')}
      </div>
      <p className="asr-service-note">上游版本未知时会生成接入修订。接口协议必须与服务实际支持的请求和返回格式一致。</p>
      <details className="asr-config-advanced"><summary>连接与认证设置</summary><div className="asr-config-grid">
        {text('apiKeyEnv', 'API Key 环境变量名（可选）', false, '例如 ASR_API_KEY')}
        {text('caFile', 'CA 证书文件（可选）')}
        {text('insecureTlsOrigin', 'TLS 例外 Origin（可选）', false, '仅填写明确允许跳过校验的 HTTPS Origin')}
        <label><span className="field-label">连接超时（秒）</span><input aria-label="连接超时（秒）" type="number" min="0.1" step="0.1" max="30" value={draft.connectTimeoutSeconds} disabled={busy} onChange={event => change('connectTimeoutSeconds', Number(event.target.value))} /></label>
        <label><span className="field-label">调用总超时（秒）</span><input aria-label="调用总超时（秒）" type="number" min="0.1" step="0.1" max="900" value={draft.totalTimeoutSeconds} disabled={busy} onChange={event => change('totalTimeoutSeconds', Number(event.target.value))} /></label>
      </div><p className="asr-service-note">认证只填写后端环境变量名，密钥值由你在运行环境设置。TLS 默认校验证书；例外必须与服务 Origin 精确匹配。</p></details>
      <div className="asr-config-actions">
        <button className="button button-primary" type="submit" disabled={!valid || busy}>{pending === 'save' ? '正在保存…' : '保存配置'}</button>
        <button className="button button-secondary" type="button" disabled={!valid || busy} onClick={() => void testDraft()}>{testing ? '正在测试连接…' : '测试连接'}</button>
        <button className="button button-secondary" type="button" disabled={!original || !clean || busy || Boolean(error)} onClick={async () => {
          if (original && await select(original.serviceId, original.revision)) setNotice('已选用此修订。请重启平台使配置生效；保存后继续编辑不会改变已选修订。')
        }}>选用此修订</button>
        {original && <button className="button button-quiet" type="button" disabled={busy} onClick={() => setDeleteConfirm(value => !value)}>删除配置</button>}
      </div>
    </form>
    {deleteConfirm && <div className="asr-service-command-notice"><p>删除“{original?.name}”的保存配置？正在使用或已选用的配置由后台检查，已有任务仍保留。</p><button type="button" className="button button-secondary" disabled={busy} onClick={() => void deleteDraft()}>确认删除配置</button><button type="button" className="text-button" onClick={() => setDeleteConfirm(false)}>取消</button></div>}
    {testResult && <div className={`asr-config-test ${testResult.success ? 'is-success' : 'is-failed'}`} role="status">
      <strong>{testResult.success ? '连接测试通过' : '连接测试未通过'}</strong><p>{testResult.message}</p>
      <p>耗时 {Math.round(testResult.durationMs)} ms{testResult.apiVersion ? ` · API ${testResult.apiVersion}` : ''}{testResult.errorCode ? ` · ${testResult.errorCode}` : ''}</p>
      <p className="asr-service-note">这是本次健康检查结果，尚未验证音频转写；修改配置后需重新测试。</p>
    </div>}
    <ErrorNotice error={testError} title="连接测试未确认" />
    <ErrorNotice error={actionError} title="配置操作未确认，请刷新后核对" />
    {notice && <p className="asr-service-command-notice" role="status">{notice}</p>}
    <div className="asr-config-audio"><div><strong>音频测试</strong><p>使用当前运行配置，在下方选择一段短音频并提交。测试会生成正常任务，结果、耗时和失败原因可在任务详情查看。</p></div><button type="button" className="button button-secondary" disabled={!canTestAudio || busy} onClick={onTestAudio}>用当前服务测试音频</button></div>
    {state?.selectedServiceId && <button className="text-button" type="button" disabled={busy || Boolean(error)} onClick={async () => {
      if (await select(null, state.selectedRevision)) setNotice('下次启动将恢复原环境配置；这不会立即关闭当前服务。')
    }}>下次启动恢复原环境配置</button>}
  </section>
}

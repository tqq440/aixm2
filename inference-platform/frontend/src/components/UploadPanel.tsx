import { useRef, useState } from 'react'
import type { FormEvent, DragEvent } from 'react'
import type { ModelSpec, TaskReceipt } from '../types/api'
import { bytes, languageName } from '../utils/format'
import { hasUnknownUpstreamVersion } from '../utils/modelVersion'
import { ErrorNotice } from './Feedback'
import type { ErrorInfo } from './Feedback'
import { Icon } from './Icon'

interface Props {
  models: ModelSpec[]; modelError: ErrorInfo | null; submitting: boolean
  error: ErrorInfo | null; receipt: TaskReceipt | null; uncertain: boolean
  serviceBlock?: string | null
  serviceModel?: { modelId: string; modelVersion: string } | null
  externalBlock?: string | null
  onSubmit: (file: File, modelId: string, modelVersion: string, language: string | null) => Promise<TaskReceipt | null>
  onReset: () => void
}

const modelKey = (model: ModelSpec) => JSON.stringify([model.modelId, model.modelVersion])

export function UploadPanel({ models, modelError, submitting, error, receipt, uncertain, serviceBlock, serviceModel, externalBlock, onSubmit, onReset }: Props) {
  const [file, setFile] = useState<File | null>(null)
  const [selectedModelKey, setSelectedModelKey] = useState('')
  // A catalog refresh may replace its default or remove a disabled model. Retrying
  // an unconfirmed upload must still use the exact version originally submitted.
  const [submittedModel, setSubmittedModel] = useState<ModelSpec | null>(null)
  const [language, setLanguage] = useState('zh')
  const [localError, setLocalError] = useState<string | null>(null)
  const [dragging, setDragging] = useState(false)
  const input = useRef<HTMLInputElement>(null)
  const model = submittedModel ?? models.find(item => modelKey(item) === selectedModelKey) ?? models[0]
  const available = Boolean(model && models.some(item => modelKey(item) === modelKey(model)))
  const retryOriginal = Boolean(submittedModel && uncertain)
  const external = model?.adapterKey === 'remote-v2-sensevoice'
  const integrationRevision = hasUnknownUpstreamVersion(model)
  const modelMismatch = Boolean(!external && model && serviceModel && (model.modelId !== serviceModel.modelId || model.modelVersion !== serviceModel.modelVersion))
  const blocked = external ? externalBlock : serviceBlock || (modelMismatch ? '所选模型版本与当前 ASR 实例不同，请选择该实例对应的模型版本。' : null)
  // Lost upload responses still need the same idempotency key to query/replay an
  // already accepted task, even after its service has stopped.
  const canSubmit = Boolean(model && (available || retryOriginal) && (!modelError || retryOriginal) && (!blocked || retryOriginal))
  const fileError = file?.size === 0 ? '这个文件是空的，请选择有效音频。'
    : file && model && file.size > model.inputConstraints.maxFileBytes
      ? `文件超出 ${bytes(model.inputConstraints.maxFileBytes)} 上限，请选择较小的音频。` : null
  const validationError = localError ?? fileError
  const clearAttempt = () => { onReset(); setSubmittedModel(null) }

  const choose = (files: FileList | null) => {
    if (submitting || !files?.length) return
    clearAttempt()
    if (files.length !== 1) { setLocalError('每次请选择一个音频文件。'); return }
    const next = files[0]
    setFile(next)
    setLocalError(null)
  }
  const drop = (event: DragEvent<HTMLLabelElement>) => {
    event.preventDefault(); setDragging(false); choose(event.dataTransfer.files)
  }
  const reset = () => { clearAttempt(); setFile(null); setLocalError(null); if (input.current) input.current.value = '' }
  const submit = (event: FormEvent) => {
    event.preventDefault()
    if (file && model && canSubmit && !validationError && !submitting && !receipt) {
      setSubmittedModel(model)
      void onSubmit(file, model.modelId, model.modelVersion, external ? null : language)
    }
  }

  return <section className="panel upload-panel" aria-labelledby="upload-title">
    <div className="panel-heading"><div><span className="section-kicker">NEW TRANSCRIPTION</span><h2 id="upload-title">新建转写任务</h2></div><span className="step-number">01</span></div>
    <p className="panel-intro">将声音转成可阅读、可下载的文字。</p>
    <form onSubmit={submit}>
      <input ref={input} id="audio-upload" aria-label="选择音频" type="file" accept="audio/*,.wav,.mp3,.m4a,.flac,.ogg,.opus,.aac,.wma,.mp4" className="file-input" disabled={submitting} onChange={event => choose(event.target.files)} />
      <label htmlFor="audio-upload" className={`dropzone ${dragging ? 'is-dragging' : ''} ${file ? 'has-file' : ''} ${submitting ? 'is-disabled' : ''}`} onDragOver={event => { event.preventDefault(); if (!submitting) setDragging(true) }} onDragLeave={() => setDragging(false)} onDrop={drop}>
        <span className="upload-symbol"><Icon name={file ? 'audio' : 'upload'} size={27} /></span>
        {file ? <><strong className="chosen-file">{file.name}</strong><span>{bytes(file.size)} · 点击更换文件</span></> : <><strong>拖拽音频到这里</strong><span>或<span className="inline-accent">点击选择文件</span></span></>}
        <small>WAV、MP3、M4A、FLAC 等音频格式</small>
      </label>
      <div className="input-limits"><Icon name="info" size={14} /><span>{model ? `最大 ${bytes(model.inputConstraints.maxFileBytes)} · 最长 ${Math.floor(model.inputConstraints.maxAudioSeconds / 60)} 分 ${Math.round(model.inputConstraints.maxAudioSeconds % 60)} 秒` : '连接后台后读取文件限制'}</span></div>
      <label className="field-label" htmlFor="model">转写模型</label>
      <select id="model" aria-label="模型" value={model ? modelKey(model) : ''} disabled={submitting || !model} onChange={event => { clearAttempt(); setSelectedModelKey(event.target.value) }}>
        {!model && <option value="">暂无可提交的模型</option>}
        {submittedModel && !available && <option value={modelKey(submittedModel)} disabled>{submittedModel.modelId} · 原提交{integrationRevision ? '接入修订' : '版本'}</option>}
        {models.map(item => <option key={modelKey(item)} value={modelKey(item)}>{item.modelId === 'whisper-small' ? 'Whisper small · 多语言' : `${item.modelId}${item.adapterKey === 'remote-v2-sensevoice' ? ' · 外部服务' : ''}`}</option>)}
      </select>
      {model && <p className="field-hint model-version">{integrationRevision ? `上游模型版本未知 · ${submittedModel ? '本次提交接入修订' : '接入修订'}` : submittedModel ? '本次提交版本' : '模型版本'}：<code>{model.modelVersion}</code></p>}
      {!models.length && !modelError && <p className="field-hint" role="status">暂无已启用且可提交的模型。启用后点击“刷新任务”重新读取；已有任务仍可查看和下载。</p>}
      {submittedModel && !available && <p className="field-hint">原提交{integrationRevision ? '接入修订' : '版本'}已不在可提交目录中。{uncertain ? integrationRevision ? '仍可使用原接入修订和提交编号重试确认；不会改用其他接入配置。' : '仍可使用原版本和提交编号重试确认；不会改用其他版本。' : '新任务请在目录恢复后重新选择模型。'}</p>}
      {external ? <p className="field-hint">语言由服务端决定，平台不传入语言参数。仅排队且尚未领取时可以取消；开始调用后不支持取消，也不会自动重发。</p> : <><label className="field-label" htmlFor="language">识别语言</label>
      <select id="language" aria-label="识别语言" value={language} disabled={submitting || !model} onChange={event => { clearAttempt(); setLanguage(event.target.value) }}>
        <option value="auto">自动识别</option>
        {model?.parameterSchema.languages.map(code => <option key={code} value={code}>{languageName(code)}</option>)}
      </select>
      <p className="field-hint">已知音频语言时，建议直接选择对应语言。</p></>}
      <ErrorNotice error={modelError} title="模型目录读取失败" />
      <ErrorNotice error={validationError ? { message: validationError } : error} />
      {blocked && !receipt && <p className="field-hint" role="status">{blocked}{retryOriginal ? ' 可重试确认原提交；不会改用其他模型或新建提交编号。' : ' 已有任务与结果仍可查看。'}</p>}
      {uncertain && <p className="uncertain-note">尚未确认后台是否收到任务。请用下方按钮重试，系统会避免重复创建同一任务。</p>}
      {receipt && <div className="submission-success" role="status"><Icon name="check" size={18} /><div><strong>{receipt.replayed ? '已找到先前提交的任务' : '任务已提交'}</strong><p>可在任务列表查看处理结果。</p></div></div>}
      <button className="button button-primary submit-button" type="submit" disabled={!file || !canSubmit || Boolean(validationError) || submitting || Boolean(receipt)}>
        <span>{submitting ? '正在上传并提交…' : receipt ? '已提交' : error ? '重试提交' : '提交转写'}</span><Icon name={submitting ? 'refresh' : receipt ? 'check' : 'arrow'} size={18} className={submitting ? 'spin' : undefined} />
      </button>
      {receipt && <button className="text-button new-task-button" type="button" onClick={reset}>提交新任务</button>}
    </form>
    <div className="privacy-note"><span className="tiny-dot" /><p>{external ? '音频会发送到后台配置的外部 ASR 服务，并在本机保留任务和结果。关闭页面不会取消远端调用。' : '音频保存在本机。提交完成后，关闭页面不会中断后台任务。'}</p></div>
  </section>
}

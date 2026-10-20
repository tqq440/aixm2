import { useEffect, useRef, useState } from 'react'
import type { DragEvent, FormEvent } from 'react'
import type { TaskReceipt, VisionModelSpec, VisionParameters, VisionTaskView } from '../types/api'
import { bytes } from '../utils/format'
import { ErrorNotice } from './Feedback'
import type { ErrorInfo } from './Feedback'
import { Icon } from './Icon'

interface Props {
  models: VisionModelSpec[]; modelError: ErrorInfo | null; submitting: boolean
  error: ErrorInfo | null; receipt: TaskReceipt<VisionTaskView> | null; uncertain: boolean
  onSubmit: (file: File, modelId: string, modelVersion: string, parameters: VisionParameters) => Promise<TaskReceipt<VisionTaskView> | null>
  onReset: () => void
}
type ParameterFields = { [K in keyof VisionParameters]: string }
const modelKey = (model: VisionModelSpec) => JSON.stringify([model.modelId, model.modelVersion])

export function VisionUploadPanel({ models, modelError, submitting, error, receipt, uncertain, onSubmit, onReset }: Props) {
  const [file, setFile] = useState<File | null>(null)
  const [selectedModelKey, setSelectedModelKey] = useState('')
  const [submittedModel, setSubmittedModel] = useState<VisionModelSpec | null>(null)
  const [fields, setFields] = useState<ParameterFields | null>(null)
  const [localError, setLocalError] = useState<string | null>(null)
  const [fileCheck, setFileCheck] = useState<{ file: File; limit: number; error: string | null; url: string | null } | null>(null)
  const [previewFailed, setPreviewFailed] = useState(false)
  const [dragging, setDragging] = useState(false)
  const input = useRef<HTMLInputElement>(null)
  const model = submittedModel ?? models.find(item => modelKey(item) === selectedModelKey) ?? models[0]
  const available = Boolean(model && models.some(item => modelKey(item) === modelKey(model)))
  const retryOriginal = Boolean(submittedModel && uncertain)
  const canSubmit = Boolean(model && (available || retryOriginal) && (!modelError || retryOriginal))
  const values: ParameterFields = fields ?? {
    confidenceThreshold: String(model?.parameterSchema.defaultConfidenceThreshold ?? 0.25),
    iouThreshold: String(model?.parameterSchema.defaultIouThreshold ?? 0.45),
    maxDetections: String(model?.parameterSchema.defaultMaxDetections ?? 100),
  }
  const parameters = {
    confidenceThreshold: Number(values.confidenceThreshold), iouThreshold: Number(values.iouThreshold),
    maxDetections: Number(values.maxDetections),
  }
  const maxDetections = model?.parameterSchema.maxDetections ?? 300
  const parameterError = !Number.isFinite(parameters.confidenceThreshold) || parameters.confidenceThreshold <= 0 || parameters.confidenceThreshold > 1
    ? '置信度阈值必须大于 0 且不超过 1。'
    : !Number.isFinite(parameters.iouThreshold) || parameters.iouThreshold <= 0 || parameters.iouThreshold > 1
      ? '重叠阈值必须大于 0 且不超过 1。'
      : !Number.isInteger(parameters.maxDetections) || parameters.maxDetections < 1 || parameters.maxDetections > maxDetections
        ? `最多目标数必须为 1 到 ${maxDetections} 的整数。` : null
  const previewByteLimit = model?.inputConstraints.maxFileBytes ?? 10 * 1024 * 1024
  const fileError = file?.size === 0 ? '这个文件是空的，请选择有效图片。'
    : file && file.size > previewByteLimit
      ? `文件超出 ${bytes(previewByteLimit)} 上限，请选择较小的图片。` : null
  const checked = fileCheck?.file === file && fileCheck.limit === previewByteLimit ? fileCheck : null
  const validationError = localError ?? fileError ?? checked?.error ?? parameterError
  const checking = Boolean(file && !checked && !fileError)

  useEffect(() => {
    if (!file || file.size === 0 || file.size > previewByteLimit) return
    // Only inspect the small signature here. The backend is authoritative for
    // complete decoding, animation and EXIF-normalized pixel limits.
    const reader = new FileReader()
    let live = true
    let url: string | null = null
    reader.onload = () => {
      if (!live) return
      const header = new Uint8Array(reader.result as ArrayBuffer)
      const jpeg = header[0] === 0xff && header[1] === 0xd8 && header[2] === 0xff
      const png = [137, 80, 78, 71, 13, 10, 26, 10].every((value, index) => header[index] === value)
      if (!jpeg && !png) {
        setFileCheck({ file, limit: previewByteLimit, error: '仅支持静态 JPEG 或 PNG 图片；文件内容与支持的格式不符。', url: null })
        return
      }
      url = URL.createObjectURL(new Blob([file], { type: jpeg ? 'image/jpeg' : 'image/png' }))
      setFileCheck({ file, limit: previewByteLimit, error: null, url })
    }
    reader.onerror = () => { if (live) setFileCheck({ file, limit: previewByteLimit, error: '无法读取这个文件，请重新选择图片。', url: null }) }
    reader.readAsArrayBuffer(file.slice(0, 8))
    return () => { live = false; if (reader.readyState === FileReader.LOADING) reader.abort(); if (url) URL.revokeObjectURL(url) }
  }, [file, previewByteLimit])

  const clearAttempt = () => { onReset(); setSubmittedModel(null) }
  const choose = (files: FileList | null) => {
    if (submitting || !files?.length) return
    clearAttempt(); setPreviewFailed(false); setFileCheck(null)
    if (files.length !== 1) { setFile(null); setLocalError('每次请选择一张图片。'); return }
    setFile(files[0]); setLocalError(null)
  }
  const drop = (event: DragEvent<HTMLLabelElement>) => { event.preventDefault(); setDragging(false); choose(event.dataTransfer.files) }
  const reset = () => { clearAttempt(); setFile(null); setFileCheck(null); setPreviewFailed(false); setLocalError(null); if (input.current) input.current.value = '' }
  const changeParameter = (key: keyof VisionParameters, value: string) => { clearAttempt(); setFields({ ...values, [key]: value }) }
  const submit = (event: FormEvent) => {
    event.preventDefault()
    if (file && model && canSubmit && !validationError && !checking && !submitting && !receipt) {
      // Preserve every parameter alongside the exact catalog version for an
      // uncertain-response replay; catalog refreshes must not change this request.
      setSubmittedModel(model); setFields({ ...values })
      void onSubmit(file, model.modelId, model.modelVersion, parameters)
    }
  }

  return <section className="panel upload-panel vision-upload-panel" aria-labelledby="vision-upload-title">
    <div className="panel-heading"><div><span className="section-kicker">NEW IMAGE DETECTION</span><h2 id="vision-upload-title">新建图片检测任务</h2></div><span className="step-number">01</span></div>
    <p className="panel-intro">上传一张图片，查看目标类别与检测框。</p>
    <form onSubmit={submit}>
      <input ref={input} id="image-upload" aria-label="选择图片" type="file" accept="image/jpeg,image/png,.jpg,.jpeg,.png" className="file-input" disabled={submitting} onChange={event => { choose(event.target.files); event.target.value = '' }} />
      <label htmlFor="image-upload" className={`dropzone vision-dropzone ${dragging ? 'is-dragging' : ''} ${file ? 'has-file' : ''} ${submitting ? 'is-disabled' : ''}`} onDragOver={event => { event.preventDefault(); if (!submitting) setDragging(true) }} onDragLeave={() => setDragging(false)} onDrop={drop}>
        {checked?.url && !previewFailed ? <img className="vision-upload-preview" src={checked.url} alt="待提交图片预览" onError={() => setPreviewFailed(true)} /> : <span className="upload-symbol"><Icon name="image" size={27} /></span>}
        {file ? <><strong className="chosen-file">{file.name}</strong><span>{bytes(file.size)} · 点击更换图片</span></> : <><strong>拖拽图片到这里</strong><span>或<span className="inline-accent">点击选择文件</span></span></>}
        <small>单张静态 JPEG / PNG</small>
      </label>
      {file && <button className="text-button vision-remove-file" type="button" disabled={submitting} onClick={reset}>移除图片</button>}
      {checking && <p className="vision-preview-note" role="status">正在检查图片格式…</p>}
      {previewFailed && <p className="vision-preview-note">浏览器无法预览此图片，提交后由后台检查是否能够解码。</p>}
      <div className="input-limits"><Icon name="info" size={14} /><span>{model ? `最大 ${bytes(model.inputConstraints.maxFileBytes)} · ${model.inputConstraints.maxPixels.toLocaleString('zh-CN')} 像素` : '连接后台后读取图片限制'}</span></div>
      <label className="field-label" htmlFor="vision-model">检测模型</label>
      <select id="vision-model" value={model ? modelKey(model) : ''} disabled={submitting || !model} onChange={event => { clearAttempt(); setSelectedModelKey(event.target.value); setFields(null) }}>
        {!model && <option value="">暂无可提交的模型</option>}
        {submittedModel && !available && <option value={modelKey(submittedModel)} disabled>{submittedModel.modelId} · 原提交版本</option>}
        {models.map(item => <option key={modelKey(item)} value={modelKey(item)}>{item.modelId === 'yolo-general' ? 'YOLO · 通用目标检测' : item.modelId}</option>)}
      </select>
      {model && <p className="field-hint model-version">{submittedModel ? '本次提交版本' : '模型版本'}：<code>{model.modelVersion}</code></p>}
      {!models.length && !modelError && <p className="field-hint" role="status">暂无已启用且可提交的检测模型。启用后刷新任务重新读取；已有任务仍可查看和下载。</p>}
      {submittedModel && !available && <p className="field-hint">原提交版本已不在可提交目录中。{uncertain ? '仍可使用原版本、参数和提交编号重试确认；不会改用其他版本。' : '新任务请在目录恢复后重新选择模型。'}</p>}
      <div className="vision-parameters">
        <div><label className="field-label" htmlFor="vision-confidence">置信度阈值</label><input id="vision-confidence" type="number" min="0" max="1" step="any" required disabled={submitting || !model} value={values.confidenceThreshold} onChange={event => changeParameter('confidenceThreshold', event.target.value)} /><p>越高，保留的目标越少。</p></div>
        <div><label className="field-label" htmlFor="vision-iou">重叠阈值（IoU）</label><input id="vision-iou" type="number" min="0" max="1" step="any" required disabled={submitting || !model} value={values.iouThreshold} onChange={event => changeParameter('iouThreshold', event.target.value)} /><p>控制重叠检测框的筛选。</p></div>
        <div className="vision-parameter-wide"><label className="field-label" htmlFor="vision-max-detections">最多目标数</label><input id="vision-max-detections" type="number" min="1" max={maxDetections} step="1" required disabled={submitting || !model} value={values.maxDetections} onChange={event => changeParameter('maxDetections', event.target.value)} /><p>取值 1–{maxDetections}；没有目标也可以成功。</p></div>
      </div>
      <ErrorNotice error={modelError} title="模型目录读取失败" />
      <ErrorNotice error={validationError ? { message: validationError } : error} />
      {uncertain && <p className="uncertain-note">尚未确认后台是否收到任务。请用下方按钮按原文件和参数重试，避免重复创建任务；修改内容会开始一次新提交。</p>}
      {receipt && <div className="submission-success" role="status"><Icon name="check" size={18} /><div><strong>{receipt.replayed ? '已找到先前提交的任务' : '任务已提交'}</strong><p>可在图片任务列表查看处理结果。</p></div></div>}
      <button className="button button-primary submit-button" type="submit" disabled={!file || !canSubmit || Boolean(validationError) || checking || submitting || Boolean(receipt)}><span>{submitting ? '正在上传并提交…' : receipt ? '已提交' : error ? '重试提交' : '提交检测'}</span><Icon name={submitting ? 'refresh' : receipt ? 'check' : 'arrow'} size={18} className={submitting ? 'spin' : undefined} /></button>
      {receipt && <button className="text-button new-task-button" type="button" onClick={reset}>提交新图片任务</button>}
    </form>
    <div className="privacy-note"><span className="tiny-dot" /><p>图片保存在本机。后台检查完整图片并执行检测；提交完成后关闭页面不会中断任务。</p></div>
  </section>
}

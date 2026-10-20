export function bytes(value: number) {
  return value < 1024 * 1024 ? `${(value / 1024).toFixed(1)} KB` : `${(value / (1024 * 1024)).toFixed(1)} MB`
}

export function dateTime(value: string | null | undefined) {
  if (!value) return '—'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return '—'
  return new Intl.DateTimeFormat('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false }).format(date)
}

export function seconds(value: number | null | undefined) {
  return value == null ? '未报告' : `${value.toFixed(2)} 秒`
}

export function timestamp(value: number) {
  const minutes = Math.floor(value / 60).toString().padStart(2, '0')
  return `${minutes}:${(value % 60).toFixed(2).padStart(5, '0')}`
}

const languageNames = new Intl.DisplayNames(['zh-CN'], { type: 'language' })
export function languageName(code: string | null) {
  if (!code || code === 'auto') return '自动识别'
  try { return `${languageNames.of(code) ?? code} (${code})` } catch { return code }
}

export function downloadText(fileName: string, content: string, type: string) {
  downloadBlob(fileName, new Blob([content], { type }))
}

export function downloadBlob(fileName: string, blob: Blob) {
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = fileName
  document.body.append(link)
  link.click()
  link.remove()
  // Give the browser time to consume the URL before releasing it.
  window.setTimeout(() => URL.revokeObjectURL(url), 1000)
}

export function resultFileName(original: string, taskId: string) {
  const base = original.replace(/\.[^.]+$/, '').replace(/[<>:"/\\|?*\x00-\x1f]/g, '_').slice(0, 80) || 'transcription'
  return `${base}-${taskId.slice(0, 8)}`
}

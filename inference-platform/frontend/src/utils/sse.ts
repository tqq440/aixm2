/** Bounded SSE framing only; it never reconnects or retries a request. */
export async function readSse(
  stream: ReadableStream<Uint8Array>, signal: AbortSignal,
  onEvent: (name: string, data: string) => boolean,
): Promise<void> {
  const reader = stream.getReader()
  const decoder = new TextDecoder('utf-8', { fatal: true })
  let buffer = '', eventName = '', data: string[] = [], frameSize = 0
  const limit = 256 * 1024
  const abort = () => { void reader.cancel().catch(() => {}) }
  signal.addEventListener('abort', abort, { once: true })
  try {
    while (!signal.aborted) {
      const chunk = await reader.read()
      buffer += decoder.decode(chunk.value, { stream: !chunk.done })
      while (true) {
        const end = buffer.search(/[\r\n]/)
        if (end < 0 || (!chunk.done && buffer[end] === '\r' && end === buffer.length - 1)) break
        const line = buffer.slice(0, end)
        const separatorSize = buffer[end] === '\r' && buffer[end + 1] === '\n' ? 2 : 1
        buffer = buffer.slice(end + separatorSize)
        frameSize += line.length
        if (frameSize > limit) throw new Error('SSE frame exceeds limit')
        if (!line) {
          if (data.length && !onEvent(eventName || 'message', data.join('\n'))) return
          eventName = ''; data = []; frameSize = 0
        } else if (!line.startsWith(':')) {
          const colon = line.indexOf(':')
          const field = colon < 0 ? line : line.slice(0, colon)
          let value = colon < 0 ? '' : line.slice(colon + 1)
          if (value.startsWith(' ')) value = value.slice(1)
          if (field === 'event') eventName = value
          else if (field === 'data') data.push(value)
        }
      }
      if (buffer.length + frameSize > limit) throw new Error('SSE frame exceeds limit')
      if (chunk.done) return // An unterminated final frame is not a confirmed event.
    }
  } finally {
    signal.removeEventListener('abort', abort)
    await reader.cancel().catch(() => {})
    reader.releaseLock()
  }
}

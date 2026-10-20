import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from '../api/client'
import type { useLlm } from '../hooks/useLlm'
import { llmModel, llmStatus, onlineRecord, ONLINE_ID } from '../test/llmFixtures'
import * as format from '../utils/format'
import { LlmWorkspace } from './LlmWorkspace'

type Platform = ReturnType<typeof useLlm>
function platform(overrides: Partial<Platform> = {}): Platform {
  return { status: llmStatus, models: [llmModel], statusError: null, modelsError: null, list: { items: [], total: 0, page: 1, pageSize: 20 }, listError: null,
    page: 1, setPage: vi.fn(), selectedId: null, record: null, recordError: null, output: null, streamError: null, cancelError: null,
    stopNote: '', running: false, cancelling: false, loading: false, refresh: vi.fn(), select: vi.fn(), start: vi.fn().mockResolvedValue(undefined), stop: vi.fn(), ...overrides }
}
beforeEach(() => { vi.spyOn(format, 'downloadText').mockImplementation(() => {}) })
afterEach(cleanup)

describe('LLM workspace without a production model', () => {
  it('shows unconfigured status and no invented model or answer, and blocks form submission', () => {
    const value = platform({ status: { ...llmStatus, configured: false, availability: 'unconfigured', message: '尚未配置真实大模型服务' }, models: [] })
    render(<LlmWorkspace platform={value} />)
    expect(screen.getByRole('heading', { name: '尚未配置大模型服务' })).toBeTruthy()
    expect(screen.getByRole('option').textContent).toBe('暂无可用的在线模型')
    fireEvent.change(screen.getByLabelText('输入内容'), { target: { value: '现在可以生成吗？' } })
    const button = screen.getByRole('button', { name: '开始生成' }) as HTMLButtonElement
    expect(button.disabled).toBe(true)
    fireEvent.submit(button.closest('form')!)
    expect(value.start).not.toHaveBeenCalled()
    expect(screen.queryByTestId('llm-response-text')).toBeNull()
    expect(screen.queryByText(llmModel.modelId)).toBeNull()
  })

  it('submits only explicit user text, exact model version and validated numeric parameters', () => {
    const value = platform()
    render(<LlmWorkspace platform={value} />)
    expect(screen.getByText('已配置在线服务 · 实际可用性待验证')).toBeTruthy()
    expect(screen.getByText(llmModel.modelVersion)).toBeTruthy()
    fireEvent.change(screen.getByLabelText('输入内容'), { target: { value: '请处理这段文字。' } })
    fireEvent.change(screen.getByLabelText('温度'), { target: { value: '0' } })
    fireEvent.change(screen.getByLabelText('最多输出 token'), { target: { value: '100' } })
    fireEvent.click(screen.getByRole('button', { name: '开始生成' }))
    expect(value.start).toHaveBeenCalledWith({ modelId: llmModel.modelId, modelVersion: llmModel.modelVersion, messages: [{ role: 'user', content: '请处理这段文字。' }], parameters: { temperature: 0, maxTokens: 100 } })
  })

  it.each([['温度', '-0.1'], ['温度', '2.1'], ['温度', ''], ['最多输出 token', '0'], ['最多输出 token', '2049'], ['最多输出 token', '1.5']])('rejects invalid %s value %s', (name, content) => {
    const value = platform()
    render(<LlmWorkspace platform={value} />)
    fireEvent.change(screen.getByLabelText('输入内容'), { target: { value: 'test' } })
    fireEvent.change(screen.getByLabelText(name), { target: { value: content } })
    expect(screen.getByRole('alert')).toBeTruthy()
    const button = screen.getByRole('button', { name: '开始生成' }) as HTMLButtonElement
    expect(button.disabled).toBe(true); fireEvent.submit(button.closest('form')!)
    expect(value.start).not.toHaveBeenCalled()
  })

  it('counts Unicode characters consistently and follows the catalog input limit', () => {
    const value = platform({ models: [{ ...llmModel, inputConstraints: { ...llmModel.inputConstraints, maxInputCharacters: 2 } }] })
    render(<LlmWorkspace platform={value} />)
    fireEvent.change(screen.getByLabelText('输入内容'), { target: { value: '🙂中' } })
    expect((screen.getByRole('button', { name: '开始生成' }) as HTMLButtonElement).disabled).toBe(false)
    fireEvent.change(screen.getByLabelText('输入内容'), { target: { value: '🙂中文' } })
    expect(screen.getByRole('alert').textContent).toContain('字符数')
    expect((screen.getByRole('button', { name: '开始生成' }) as HTMLButtonElement).disabled).toBe(true)
  })

  it('blocks stale configuration after a read error and blocks an unfinished existing request', () => {
    const value = platform({ modelsError: new ApiError('目录读取失败') })
    const view = render(<LlmWorkspace platform={value} />)
    fireEvent.change(screen.getByLabelText('输入内容'), { target: { value: 'test' } })
    expect((screen.getByRole('button', { name: '开始生成' }) as HTMLButtonElement).disabled).toBe(true)
    view.rerender(<LlmWorkspace platform={platform({ selectedId: ONLINE_ID, record: onlineRecord({ state: 'streaming' }) })} />)
    expect(screen.getByText(/当前请求尚未结束/)).toBeTruthy()
    expect((screen.getByRole('button', { name: '开始生成' }) as HTMLButtonElement).disabled).toBe(true)
  })

  it('renders only received text safely and labels partial downloads without inventing a completed answer', async () => {
    const text = '<img src=x onerror=alert(1)>\n部分文字'
    const clipboard = { writeText: vi.fn().mockResolvedValue(undefined) }
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: clipboard })
    const value = platform({ selectedId: ONLINE_ID, record: onlineRecord({ state: 'failed' }), output: { id: ONLINE_ID, text, complete: false, interrupted: true }, streamError: new ApiError('连接断开', 'STREAM_INTERRUPTED', 0, 'stream-request') })
    const view = render(<LlmWorkspace platform={value} />)
    expect(screen.getByTestId('llm-response-text').textContent).toBe(text)
    expect(screen.queryByRole('img')).toBeNull()
    expect(screen.getByText(/本次输出不完整/)).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: '下载部分 TXT' }))
    expect(format.downloadText).toHaveBeenCalledWith('llm-aaaaaaaa-partial.txt', text, 'text/plain;charset=utf-8')
    fireEvent.click(screen.getByRole('button', { name: '复制已接收文字' }))
    await waitFor(() => expect(clipboard.writeText).toHaveBeenCalledWith(text))
    view.rerender(<LlmWorkspace platform={{ ...value, streamError: null, record: onlineRecord({ state: 'completed' }), output: { id: ONLINE_ID, text, complete: true, interrupted: false } }} />)
    fireEvent.click(screen.getByRole('button', { name: '下载 TXT' }))
    expect(format.downloadText).toHaveBeenLastCalledWith('llm-aaaaaaaa.txt', text, 'text/plain;charset=utf-8')
  })

  it('restores only metadata and distinguishes accepted stop from confirmed cancellation', () => {
    const value = platform({ selectedId: ONLINE_ID, record: onlineRecord({ state: 'streaming', cancelRequested: true, cancellationConfirmed: false, outputCharacters: 100 }) })
    render(<LlmWorkspace platform={value} />)
    expect(screen.getByText(/这里只恢复了请求状态/)).toBeTruthy()
    expect(screen.queryByTestId('llm-response-text')).toBeNull()
    expect((screen.getByRole('button', { name: '停止请求已受理' }) as HTMLButtonElement).disabled).toBe(true)
    expect(screen.getByText('尚未确认')).toBeTruthy()
    expect(screen.getByText('占用中')).toBeTruthy()
    expect(screen.queryByRole('button', { name: '下载 TXT' })).toBeNull()
  })

  it('supports history selection and pagination without replaying prompts', () => {
    const item = onlineRecord({ state: 'completed' })
    const value = platform({ list: { items: [item], total: 21, page: 1, pageSize: 20 } })
    render(<LlmWorkspace platform={value} />)
    fireEvent.click(screen.getByRole('button', { name: `查看在线请求 ${ONLINE_ID}` }))
    expect(value.select).toHaveBeenCalledWith(ONLINE_ID)
    fireEvent.click(screen.getByRole('button', { name: '下一页' }))
    expect(value.setPage).toHaveBeenCalledWith(2)
    expect(value.start).not.toHaveBeenCalled()
  })
})

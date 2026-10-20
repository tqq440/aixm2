import { beforeEach, describe, expect, it, vi } from 'vitest'
import { readOnlineRequestId, readWorkspaceUrl, writeWorkspaceUrl } from './workspace'

const taskId = 'a'.repeat(32)

beforeEach(() => window.history.replaceState({ existing: true }, '', '/?other=retained#details'))

describe('workspace URLs', () => {
  it('keeps online request ids separate from offline task ids and preserves old links', () => {
    writeWorkspaceUrl('llm', taskId)
    expect(readWorkspaceUrl()).toEqual({ taskType: 'llm', taskId: null })
    expect(readOnlineRequestId()).toBe(taskId)
    expect(new URL(window.location.href).searchParams.has('task')).toBe(false)
    writeWorkspaceUrl('asr', taskId)
    expect(readOnlineRequestId()).toBeNull()
    expect(new URL(window.location.href).searchParams.has('request')).toBe(false)
    expect(readWorkspaceUrl()).toEqual({ taskType: 'asr', taskId })
    window.history.replaceState(null, '', '/?type=llm&request=bad-id')
    expect(readOnlineRequestId()).toBeNull()
  })
  it('retains existing ASR links and ignores invalid task identifiers', () => {
    expect(readWorkspaceUrl()).toEqual({ taskType: 'asr', taskId: null })
    window.history.replaceState(null, '', `/?task=${taskId}`)
    expect(readWorkspaceUrl()).toEqual({ taskType: 'asr', taskId })
    window.history.replaceState(null, '', '/?type=vision&task=invalid')
    expect(readWorkspaceUrl()).toEqual({ taskType: 'vision', taskId: null })
  })

  it('uses type=vision and preserves unrelated URL parts and history state', () => {
    writeWorkspaceUrl('vision', taskId)
    expect(readWorkspaceUrl()).toEqual({ taskType: 'vision', taskId })
    expect(window.location.hash).toBe('#details')
    expect(new URL(window.location.href).searchParams.get('other')).toBe('retained')
    expect(window.history.state).toEqual({ existing: true })
    writeWorkspaceUrl('asr', null)
    expect(window.location.search).toBe('?other=retained')
  })

  it('pushes a tab transition once and drops malformed task ids', () => {
    const push = vi.spyOn(window.history, 'pushState')
    writeWorkspaceUrl('vision', taskId, true)
    writeWorkspaceUrl('vision', taskId, true)
    expect(push).toHaveBeenCalledTimes(1)
    writeWorkspaceUrl('vision', '../not-a-task')
    expect(readWorkspaceUrl()).toEqual({ taskType: 'vision', taskId: null })
    push.mockRestore()
  })
})

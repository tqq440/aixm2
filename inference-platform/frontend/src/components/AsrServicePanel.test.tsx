import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from '../api/client'
import type { useAsrManagement } from '../hooks/useAsrManagement'
import type { AsrInstance, ManagementStatus, RegisteredNode } from '../types/management'
import { AsrServicePanel } from './AsrServicePanel'

type Management = ReturnType<typeof useAsrManagement>
const token = 'test-only-management-value-123456789'
const node: RegisteredNode = {
  nodeId: 'a'.repeat(32), hostname: 'TEST-LAPTOP', osName: 'Windows', osVersion: 'test-version',
  architecture: 'AMD64', cpuCount: 8, gpuStatus: 'not_probed', registrationState: 'registered',
  agentStatus: 'online', registeredAt: '2026-10-14T01:00:00Z', metadataObservedAt: '2026-10-14T01:00:00Z',
}
const instance: AsrInstance = {
  instanceId: 'b'.repeat(32), nodeId: node.nodeId, instanceKey: 'asr-default', modelId: 'whisper-small',
  modelVersion: 'test-model-version', adapterKey: 'faster-whisper', executionDevice: 'cpu',
  registrationState: 'registered', runtimeState: 'unknown', managementMode: 'node_agent', registeredAt: '2026-10-14T01:00:00Z',
}
const status: ManagementStatus = {
  instanceId: instance.instanceId, nodeId: node.nodeId, managementMode: 'node_agent', state: 'ready',
  reportedState: 'ready', desiredState: 'running', agentAvailable: true, agentSessionId: 'c'.repeat(32),
  agentPid: 10, workerSessionId: 'd'.repeat(32), childPid: 11, commandRevision: 2,
  modelReady: true, acceptingTasks: true, activeTaskId: null, lastError: null,
  heartbeatAt: '2026-10-14T01:00:00Z', exitConfirmedAt: null, shutdownConfirmedAt: null, evaluatedAt: '2026-10-14T01:00:00Z',
}

function management(overrides: Partial<Management> = {}, state: Partial<ManagementStatus> = {}): Management {
  return {
    nodes: [node], instances: [instance], selectedNodeId: node.nodeId, selectedInstanceId: instance.instanceId,
    selectedInstance: instance, status: { ...status, ...state }, loading: false, error: null,
    commandError: null, commandPending: null, commandNotice: null,
    selectNode: vi.fn(), selectInstance: vi.fn(), refresh: vi.fn(),
    start: vi.fn().mockResolvedValue(undefined), stop: vi.fn().mockResolvedValue(undefined), ...overrides,
  }
}
const button = (name: string) => screen.getByRole('button', { name }) as HTMLButtonElement
const input = () => screen.getByLabelText('本机管理令牌') as HTMLInputElement
function enterToken() { fireEvent.change(input(), { target: { value: token } }) }

afterEach(cleanup)

describe('ASR service management panel', () => {
  it.each([
    ['stopped', '未启动'], ['starting', '模型加载中'], ['ready', '可以转写'], ['busy', '正在执行'],
    ['stopping', '停止中'], ['stop_unconfirmed', '停止未确认'], ['failed', '启动或运行失败'], ['unknown', '状态未确认'],
  ] as const)('shows the authoritative %s state as %s', (state, label) => {
    render(<AsrServicePanel management={management({}, { state })} active onSelectTask={vi.fn()} />)
    expect(screen.getByText(label)).toBeTruthy()
    expect(screen.getByText('CPU')).toBeTruthy()
    expect(screen.getByText('test-model-version')).toBeTruthy()
    expect(screen.getByText(/GPU 未探测/)).toBeTruthy()
  })

  it('only enables a fixed start for a stopped service with a valid token', () => {
    const model = management({}, { state: 'stopped', desiredState: 'stopped', modelReady: false, acceptingTasks: false })
    render(<AsrServicePanel management={model} active onSelectTask={vi.fn()} />)
    expect(button('启动 ASR').disabled).toBe(true)
    fireEvent.change(input(), { target: { value: 'too-short' } })
    expect(button('启动 ASR').disabled).toBe(true)
    enterToken()
    expect(button('启动 ASR').disabled).toBe(false)
    expect(button('停止 ASR').disabled).toBe(true)
    fireEvent.click(button('启动 ASR'))
    expect(model.start).toHaveBeenCalledExactlyOnceWith(token)
    expect(model.stop).not.toHaveBeenCalled()
    expect(screen.queryByText('可以转写')).toBeNull()
  })

  it.each(['starting', 'ready', 'busy'] as const)('allows stopping %s and explains graceful completion', state => {
    const model = management({}, { state })
    render(<AsrServicePanel management={model} active onSelectTask={vi.fn()} />)
    enterToken()
    expect(button('启动 ASR').disabled).toBe(true)
    expect(button('停止 ASR').disabled).toBe(false)
    expect(screen.getByText(/当前音频处理结束后退出/)).toBeTruthy()
    fireEvent.click(button('停止 ASR'))
    expect(model.stop).toHaveBeenCalledExactlyOnceWith(token)
  })

  it.each(['stopping', 'stop_unconfirmed', 'unknown'] as const)('blocks both commands while %s', state => {
    const model = management({}, { state })
    render(<AsrServicePanel management={model} active onSelectTask={vi.fn()} />)
    enterToken()
    expect(button('启动 ASR').disabled).toBe(true)
    expect(button('停止 ASR').disabled).toBe(true)
    fireEvent.click(button('启动 ASR')); fireEvent.click(button('停止 ASR'))
    expect(model.start).not.toHaveBeenCalled(); expect(model.stop).not.toHaveBeenCalled()
  })

  it.each([
    { agentAvailable: false }, { modelReady: false }, { acceptingTasks: false }, { agentSessionId: null },
  ])('does not show ready for unavailable or incomplete reports: %s', patch => {
    render(<AsrServicePanel management={management({}, patch)} active onSelectTask={vi.fn()} />)
    enterToken()
    expect(screen.queryByText('可以转写')).toBeNull()
    expect(screen.getByText('状态未确认')).toBeTruthy()
    expect(button('停止 ASR').disabled).toBe(true)
  })

  it('discards stale ready and task information on read errors', () => {
    const model = management({ error: new ApiError('连接中断') }, { activeTaskId: 'e'.repeat(32) })
    render(<AsrServicePanel management={model} active onSelectTask={vi.fn()} />)
    enterToken()
    expect(screen.queryByText('可以转写')).toBeNull()
    expect(screen.getByText('状态未确认')).toBeTruthy()
    expect(screen.getByText(/连接中断不表示服务已停止/)).toBeTruthy()
    expect(screen.queryByRole('button', { name: /查看当前任务/ })).toBeNull()
    expect(button('停止 ASR').disabled).toBe(true)
  })

  it('retains stop-unconfirmed when the Agent is unavailable', () => {
    render(<AsrServicePanel management={management({}, { state: 'stop_unconfirmed', agentAvailable: false, activeTaskId: 'e'.repeat(32) })} active onSelectTask={vi.fn()} />)
    expect(screen.getByText('停止未确认')).toBeTruthy()
    expect(screen.queryByText('未启动')).toBeNull()
    expect(screen.queryByRole('button', { name: /查看当前任务/ })).toBeNull()
    expect(screen.getByText('暂无法确认')).toBeTruthy()
  })

  it('does not treat an unloaded busy report or a new stop intent as usable', () => {
    const model = management({}, { state: 'busy', modelReady: false })
    const view = render(<AsrServicePanel management={model} active onSelectTask={vi.fn()} />)
    expect(screen.getByText('状态未确认')).toBeTruthy()
    view.rerender(<AsrServicePanel management={management({}, { state: 'busy', desiredState: 'stopped' })} active onSelectTask={vi.fn()} />)
    expect(screen.getByText('状态未确认')).toBeTruthy()
    expect(screen.queryByText('无正在执行的任务')).toBeNull()
  })

  it('shows a current task link and last error as plain text', () => {
    const select = vi.fn()
    const taskId = 'e'.repeat(32)
    const model = management({}, { state: 'busy', activeTaskId: taskId, lastError: '<script>no execution</script>' })
    const view = render(<AsrServicePanel management={model} active onSelectTask={select} />)
    fireEvent.click(button(`查看当前任务 ${taskId}`))
    expect(select).toHaveBeenCalledExactlyOnceWith(taskId)
    expect(screen.getByText(/<script>no execution<\/script>/)).toBeTruthy()
    expect(view.container.querySelector('script')).toBeNull()
  })

  it('keeps password only in memory and supports clearing it', () => {
    const localSet = vi.spyOn(Storage.prototype, 'setItem')
    const view = render(<AsrServicePanel management={management()} active onSelectTask={vi.fn()} />)
    enterToken()
    expect(input().type).toBe('password')
    expect(input().autocomplete).toBe('off')
    expect(view.container.textContent).not.toContain(token)
    expect(localSet).not.toHaveBeenCalled()
    fireEvent.click(button('清空令牌'))
    expect(input().value).toBe('')
    expect(button('停止 ASR').disabled).toBe(true)
  })

  it('clears the password after leaving ASR and on browser pagehide', () => {
    const model = management()
    const view = render(<AsrServicePanel management={model} active onSelectTask={vi.fn()} />)
    enterToken()
    view.rerender(<AsrServicePanel management={model} active={false} onSelectTask={vi.fn()} />)
    expect(screen.queryByLabelText('本机管理令牌')).toBeNull()
    view.rerender(<AsrServicePanel management={model} active onSelectTask={vi.fn()} />)
    expect(input().value).toBe('')
    enterToken()
    fireEvent(window, new Event('pagehide'))
    expect(input().value).toBe('')
  })

  it('clears the password when the selected instance changes', () => {
    const model = management()
    const view = render(<AsrServicePanel management={model} active onSelectTask={vi.fn()} />)
    enterToken()
    view.rerender(<AsrServicePanel management={{ ...model, selectedInstanceId: 'f'.repeat(32) }} active onSelectTask={vi.fn()} />)
    expect(input().value).toBe('')
  })

  it('prevents concurrent commands, does not call a received command ready, and shows operation errors', () => {
    const model = management({ status: null, commandPending: 'start', commandNotice: '启动命令已受理，等待就绪确认。' })
    const view = render(<AsrServicePanel management={model} active onSelectTask={vi.fn()} />)
    expect(screen.getByText('命令发送中')).toBeTruthy()
    expect(input().disabled).toBe(true)
    expect(button('正在发送启动…').disabled).toBe(true)
    expect(button('停止 ASR').disabled).toBe(true)
    expect(screen.queryByText('可以转写')).toBeNull()
    view.rerender(<AsrServicePanel management={{ ...model, commandPending: null, commandError: new ApiError('管理令牌无效', 'AGENT_UNAUTHORIZED', 401, 'test-request') }} active onSelectTask={vi.fn()} />)
    expect(screen.getByRole('alert').textContent).toContain('管理令牌无效')
    expect(screen.getByRole('alert').textContent).toContain('test-request')
  })

  it('shows an empty registration without inventing a node, model or running service', () => {
    const model = management({ nodes: [], instances: [], selectedNodeId: null, selectedInstanceId: null, selectedInstance: null, status: null })
    render(<AsrServicePanel management={model} active onSelectTask={vi.fn()} />)
    expect(screen.getByText('暂无已登记节点')).toBeTruthy()
    expect(screen.getByText('暂无 ASR 实例')).toBeTruthy()
    expect(screen.getByText(/登记信息不代表模型已运行/)).toBeTruthy()
    expect(screen.queryByText('CPU')).toBeNull()
    expect(button('启动 ASR').disabled).toBe(true)
  })

  it('uses only available registry selections and refreshes on request', () => {
    const secondNode = { ...node, nodeId: '1'.repeat(32), hostname: 'SECOND-TEST-NODE' }
    const secondInstance = { ...instance, instanceId: '2'.repeat(32) }
    const model = management({ nodes: [node, secondNode], instances: [instance, secondInstance] })
    render(<AsrServicePanel management={model} active onSelectTask={vi.fn()} />)
    fireEvent.change(screen.getByLabelText('计算节点'), { target: { value: secondNode.nodeId } })
    expect(model.selectNode).toHaveBeenCalledExactlyOnceWith(secondNode.nodeId)
    fireEvent.change(screen.getByLabelText('ASR 实例'), { target: { value: secondInstance.instanceId } })
    expect(model.selectInstance).toHaveBeenCalledExactlyOnceWith(secondInstance.instanceId)
    fireEvent.click(button('刷新服务状态'))
    expect(model.refresh).toHaveBeenCalledOnce()
  })
})

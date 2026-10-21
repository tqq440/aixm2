import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from '@/api/client'
import { serviceCatalogApi as api } from '@/api/service-catalog'
import { catalogEntry, catalogGroups, catalogPage } from '@/test/serviceCatalogFixtures'
import { emptyCatalogConfig, ServiceCatalogPanel } from '@/components/ServiceCatalogPanel'

vi.mock('@/api/service-catalog', () => ({ serviceCatalogApi: {
  list: vi.fn(), groups: vi.fn(), save: vi.fn(), get: vi.fn(), test: vi.fn(), activate: vi.fn(), importAsr: vi.fn(), saveGroup: vi.fn(),
} }))

beforeEach(() => {
  vi.mocked(api.list).mockResolvedValue(catalogPage())
  vi.mocked(api.groups).mockResolvedValue(catalogGroups())
  vi.mocked(api.get).mockResolvedValue(catalogEntry())
  vi.mocked(api.save).mockResolvedValue(catalogEntry())
  vi.mocked(api.test).mockResolvedValue({ success: true, message: '连接检查通过；未验证推理或设备。', errorCode: null })
  vi.mocked(api.activate).mockResolvedValue({ ...catalogEntry(), version: 2, enabled: true, activeRevision: 'a'.repeat(64) })
  vi.mocked(api.importAsr).mockResolvedValue({ imported: ['old-asr'], skipped: [], message: '原配置保持。' })
})
afterEach(() => { cleanup(); vi.resetAllMocks() })

const loaded = async () => { await screen.findByRole('button', { name: /示例语音服务/ }); await waitFor(() => expect((screen.getByRole('button', { name: '保存草稿' }) as HTMLButtonElement).disabled).toBe(false)) }
const edit = async () => { await loaded(); fireEvent.click(screen.getByRole('button', { name: /示例语音服务/ })) }

describe('public service catalog module', () => {
  it('saves an explicit immutable Node binding with credential reference only', async () => {
    render(<ServiceCatalogPanel />); await edit()
    fireEvent.click(screen.getByLabelText('通过 Node 执行'))
    for (const [label, value] of [['Node 编号', 'lab-node'], ['Node 本机服务编号', 'asr'], ['Node 配置修订（64位）', 'a'.repeat(64)], ['Node 地址', 'http://127.0.0.1:8020'], ['中心执行凭据环境变量名', 'LAB_NODE_EXECUTOR']]) {
      fireEvent.change(screen.getByLabelText(label), { target: { value } })
    }
    fireEvent.click(screen.getByRole('button', { name: '保存草稿' }))
    await waitFor(() => expect(api.save).toHaveBeenCalled())
    expect(vi.mocked(api.save).mock.calls[0][0].targets[0].node).toEqual({ nodeId: 'lab-node', serviceId: 'asr', configRevision: 'a'.repeat(64), baseUrl: 'http://127.0.0.1:8020', executorTokenEnv: 'LAB_NODE_EXECUTOR' })
  })
  it('only reads on opening and displays module boundary', async () => {
    render(<ServiceCatalogPanel />); await loaded()
    expect(screen.getByText(/保存、测试或目录启用都不会/)).toBeTruthy()
    expect(api.test).not.toHaveBeenCalled(); expect(api.activate).not.toHaveBeenCalled(); expect(api.importAsr).not.toHaveBeenCalled()
  })
  it('uses immutable saved revisions for explicit connection tests', async () => {
    render(<ServiceCatalogPanel />); await edit()
    fireEvent.click(screen.getByRole('button', { name: '测试此目标连接' }))
    await screen.findByText('连接检查通过；未验证推理或设备。')
    expect(api.test).toHaveBeenCalledWith(catalogEntry(), 'primary')
    expect(api.activate).not.toHaveBeenCalled()
    fireEvent.change(screen.getByLabelText('服务名称'), { target: { value: '未保存修改' } })
    expect((screen.getByRole('button', { name: '测试此目标连接' }) as HTMLButtonElement).disabled).toBe(true)
  })
  it('saves a draft without activating and preserves missing progress honestly', async () => {
    render(<ServiceCatalogPanel />); await edit()
    expect(screen.getByText(/服务未提供进度；不推算百分比/)).toBeTruthy()
    fireEvent.change(screen.getByLabelText('服务名称'), { target: { value: '新名称' } })
    fireEvent.click(screen.getByRole('button', { name: '保存草稿' }))
    await waitFor(() => expect(api.save).toHaveBeenCalledWith({ ...catalogEntry().config, name: '新名称' }, 1))
    expect(api.activate).not.toHaveBeenCalled()
  })
  it('keeps user edits when the server rejects a stale version', async () => {
    vi.mocked(api.save).mockRejectedValue(new ApiError('目录已被其他操作修改，请刷新。', 'CATALOG_VERSION_CONFLICT', 409))
    render(<ServiceCatalogPanel />); await edit()
    fireEvent.change(screen.getByLabelText('服务名称'), { target: { value: '保留的草稿' } })
    fireEvent.click(screen.getByRole('button', { name: '保存草稿' }))
    await screen.findByRole('alert')
    expect((screen.getByLabelText('服务名称') as HTMLInputElement).value).toBe('保留的草稿')
  })
  it('creates protocol-specific targets without stray protocol fields', async () => {
    render(<ServiceCatalogPanel />); await loaded()
    fireEvent.change(screen.getByLabelText('服务类型'), { target: { value: 'vision' } })
    expect((screen.getByLabelText('调用路径') as HTMLInputElement).value).toBe('/v1/vision/detections')
    expect((screen.getByLabelText('文件上限（MiB）') as HTMLInputElement).value).toBe('10')
    expect(emptyCatalogConfig().targets[0]).not.toHaveProperty('protocol')
    fireEvent.change(screen.getByLabelText('服务类型'), { target: { value: 'llm' } })
    expect((screen.getByLabelText('调用模式') as HTMLSelectElement).value).toBe('stream')
  })
  it('records optional monitoring declarations without probing a task', async () => {
    render(<ServiceCatalogPanel />); await edit()
    fireEvent.change(screen.getByLabelText('监控能力'), { target: { value: 'job-json-v1' } })
    expect((screen.getByLabelText('状态查询路径') as HTMLInputElement).value).toBe('/jobs/{requestId}')
    fireEvent.click(screen.getByLabelText('服务声明提供进度'))
    fireEvent.click(screen.getByRole('button', { name: '保存草稿' }))
    await waitFor(() => expect(api.save).toHaveBeenCalled())
    expect(vi.mocked(api.save).mock.calls[0][0].targets[0].monitoring.reportsProgress).toBe(true)
    expect(api.test).not.toHaveBeenCalled()
  })
  it('imports only when requested and does not activate imported services', async () => {
    render(<ServiceCatalogPanel />); await loaded()
    fireEvent.click(screen.getByRole('button', { name: '导入已有 ASR 配置' }))
    await screen.findByText('导入 1 项，跳过 0 项。原配置保持。')
    expect(api.importAsr).toHaveBeenCalledTimes(1); expect(api.activate).not.toHaveBeenCalled()
  })
  it('shows database migration errors and offers retry without false empty state', async () => {
    vi.mocked(api.list).mockRejectedValue(new ApiError('请先完成 0015 迁移。', 'CATALOG_STORAGE_UNAVAILABLE', 503))
    render(<ServiceCatalogPanel />)
    await screen.findByText('请先完成 0015 迁移。')
    expect(screen.queryByText(/暂无服务/)).toBeNull()
  })
})

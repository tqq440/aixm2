import { useEffect, useRef, useState } from 'react'
import { usePlatform } from './hooks/usePlatform'
import { useLlm } from './hooks/useLlm'
import { useAsrManagement } from './hooks/useAsrManagement'
import { useExternalAsr } from './hooks/useExternalAsr'
import { useAsrScheduling } from '@/hooks/useAsrScheduling'
import { useAsrServices } from '@/hooks/useAsrServices'
import { AsrConfigurationPanel } from '@/components/AsrConfigurationPanel'
import { AsrSchedulingPanel } from '@/components/AsrSchedulingPanel'
import { ExternalAsrPanel } from './components/ExternalAsrPanel'
import { asrSubmissionBlock } from './utils/asrService'
import { AsrServicePanel } from './components/AsrServicePanel'
import { readWorkspaceUrl, writeWorkspaceUrl } from './utils/workspace'
import type { WorkspaceType } from './utils/workspace'
import { Icon } from './components/Icon'
import { RuntimePanel } from './components/RuntimePanel'
import { UploadPanel } from './components/UploadPanel'
import { VisionUploadPanel } from './components/VisionUploadPanel'
import { TaskList } from './components/TaskList'
import { TaskDetail } from './components/TaskDetail'
import { VisionTaskDetail } from './components/VisionTaskDetail'
import { ErrorNotice } from './components/Feedback'
import { LlmWorkspace } from './components/LlmWorkspace'
import { ServiceCatalogPanel } from '@/components/ServiceCatalogPanel'
import { GatewayPanel } from '@/components/GatewayPanel'

export default function App() {
  const [taskType, setTaskType] = useState<WorkspaceType>(() => readWorkspaceUrl().taskType)
  const [catalogOpen, setCatalogOpen] = useState(false)
  const [gatewayOpen, setGatewayOpen] = useState(false)
  const asr = usePlatform('asr', taskType === 'asr' && !catalogOpen)
  const vision = usePlatform('vision', taskType === 'vision' && !catalogOpen)
  const llm = useLlm(taskType === 'llm' && !catalogOpen)
  const management = useAsrManagement(taskType === 'asr' && !catalogOpen)
  const external = useExternalAsr(taskType === 'asr' && !catalogOpen)
  const scheduling = useAsrScheduling(taskType === 'asr' && !catalogOpen)
  const services = useAsrServices(taskType === 'asr' && !catalogOpen)
  const [legacySubmission, setLegacySubmission] = useState(false)
  const externalBlock = external.error || external.stale || !external.status
    ? '服务调用状态尚未确认，请刷新调用状态后再提交。'
    : services.error || !services.state ? '服务配置尚未确认，请刷新配置后再提交。'
    : services.state.restartRequired ? '已选用新的服务配置，请重启平台后再提交新音频。'
    : external.status.available ? null : external.status.message || '服务当前不可调用，请等待状态恢复。'
  const serviceBlock = asrSubmissionBlock(management.status, Boolean(management.error), management.commandPending)
  const runningModel = services.state?.runningModel
  const interfaceModels = asr.models.filter(model => (model.managementMode === 'external_call_only' || model.adapterKey === 'remote-v2-sensevoice') && runningModel && model.modelId === runningModel.modelId && model.modelVersion === runningModel.modelVersion)
  const legacyModels = asr.models.filter(model => model.managementMode !== 'external_call_only' && model.adapterKey !== 'remote-v2-sensevoice')
  const submissionModels = legacySubmission ? legacyModels : interfaceModels
  const platform = taskType === 'vision' ? vision : asr
  const isVision = taskType === 'vision'
  const isLlm = taskType === 'llm'
  const refreshing = isLlm ? llm.loading : platform.loading
  const help = useRef<HTMLDialogElement>(null)
  const [helpOpen, setHelpOpen] = useState(false)
  useEffect(() => {
    const onPopState = () => setTaskType(readWorkspaceUrl().taskType)
    window.addEventListener('popstate', onPopState)
    return () => window.removeEventListener('popstate', onPopState)
  }, [])
  const changeWorkspace = (next: WorkspaceType) => {
    setGatewayOpen(false)
    setCatalogOpen(false)
    if (next === taskType || asr.submitting || vision.submitting) return
    writeWorkspaceUrl(next, next === 'llm' ? llm.selectedId : (next === 'vision' ? vision : asr).selectedTaskId, true)
    setTaskType(next)
  }
  const select = (id: string) => {
    platform.selectTask(id)
    if (window.matchMedia('(max-width: 760px)').matches) {
      document.getElementById(isVision ? 'vision-task-detail' : 'task-detail')?.scrollIntoView({ behavior: 'smooth', block: 'start' })
    }
  }
  const showHelp = () => { help.current?.showModal(); setHelpOpen(true) }
  return <div className="app-shell">
    <a className="skip-link" href="#workspace">跳到工作台</a>
    <aside className="sidebar">
      <a className="brand" href="#workspace" aria-label="推理平台工作台"><span className="brand-symbol"><Icon name="wave" size={27} /></span><span><strong>推理平台</strong><small>INFERENCE PLATFORM</small></span></a>
      <div className="sidebar-divider" />
      <div className="nav-group-label">工作空间</div>
      <nav aria-label="主导航">
        {([{ kind: 'asr', title: '音频转写', icon: 'wave' }, { kind: 'vision', title: '图片检测', icon: 'image' }, { kind: 'llm', title: '文本生成', icon: 'chat' }] as const).map(item => <button key={item.kind} type="button" className={`nav-link ${!catalogOpen && taskType === item.kind ? 'is-active' : ''}`} aria-current={!catalogOpen && taskType === item.kind ? 'page' : undefined} disabled={asr.submitting || vision.submitting} onClick={() => changeWorkspace(item.kind)}><Icon name={item.icon} size={19} /><span>{item.title}</span>{!catalogOpen && taskType === item.kind && <span className="nav-active-mark" />}</button>)}
        <button type="button" className={`nav-link ${catalogOpen && !gatewayOpen ? 'is-active' : ''}`} aria-current={catalogOpen && !gatewayOpen ? 'page' : undefined} disabled={asr.submitting || vision.submitting} onClick={() => { setGatewayOpen(false); setCatalogOpen(true) }}><Icon name="info" size={19} /><span>服务管理</span></button>
        <button type="button" className={`nav-link ${gatewayOpen ? 'is-active' : ''}`} aria-current={gatewayOpen ? 'page' : undefined} disabled={asr.submitting || vision.submitting} onClick={() => { setGatewayOpen(true); setCatalogOpen(true) }}><Icon name="info" size={19} /><span>应用与任务监控</span></button>
        <button type="button" className="nav-link help-link" onClick={showHelp} aria-haspopup="dialog" aria-expanded={helpOpen}><Icon name="info" size={19} /><span>使用说明</span></button>
      </nav>
      <div className="sidebar-footer"><span className="local-chip"><span />本机开发版</span><p>分布式通用 AI 推理<br />与算力调度平台</p><small>ASR · VISION · LLM</small></div>
    </aside>
    <div className="main-shell">
      <header className="topbar"><div className="breadcrumb">工作空间<Icon name="chevron" size={13} /><strong>{gatewayOpen ? '应用与任务监控' : catalogOpen ? '服务管理' : isLlm ? '文本生成' : isVision ? '图片检测' : '音频转写'}</strong></div><span className="local-label"><span className="tiny-dot" />本机访问</span></header>
      <main id="workspace" className="workspace">
        {gatewayOpen ? <GatewayPanel /> : catalogOpen && <ServiceCatalogPanel />}
        <div hidden={catalogOpen}>
        <div className="page-heading"><div><div className="page-eyebrow"><span />{isLlm ? 'ONLINE TEXT GENERATION' : isVision ? 'IMAGE DETECTION' : 'AUDIO TRANSCRIPTION'}</div><h1>{isLlm ? '与文字，实时连接' : isVision ? '看见图片中的目标' : '把声音，留成文字'}<span className="title-period">。</span></h1><p>{isLlm ? '连接真实文本服务，逐步接收输出与明确的请求状态。' : isVision ? '上传图片、查看检测框，让每一个目标清楚可见。' : '上传音频、追踪任务，让每一次转写都有据可查。'}</p></div><button type="button" className="button button-secondary refresh-button" onClick={() => isLlm ? llm.refresh() : platform.refresh()} disabled={refreshing}><Icon name="refresh" size={16} className={refreshing ? 'spin' : undefined} />{isLlm ? '刷新状态' : '刷新任务'}</button></div>
        {!isLlm && <><RuntimePanel runtime={platform.runtime} error={platform.runtimeError} loading={platform.loading} taskType={isVision ? 'vision' : 'asr'} interfaceAsr={!isVision && !legacySubmission} /><ErrorNotice error={platform.runtimeError} title="暂时无法读取后台状态" /></>}
        <div hidden={taskType !== 'asr'} data-workspace="asr">
          <AsrConfigurationPanel services={services} canTestAudio={Boolean(runningModel && interfaceModels.length && !externalBlock)} onTestAudio={() => {
            setLegacySubmission(false)
            document.getElementById('audio-test-upload')?.scrollIntoView?.({ behavior: 'smooth', block: 'start' })
            document.getElementById('audio-upload')?.focus()
          }} />
          <ExternalAsrPanel external={external} onSelectTask={select} restartRequired={services.state?.restartRequired} />
          <details className="asr-legacy-management"><summary>已有实例管理与兼容入口</summary>
            <p className="asr-service-note">保留原 Whisper 实例的管理和提交能力。新服务统一在上方按接口配置，已有任务和结果仍可查询。</p>
            <AsrServicePanel management={management} active={taskType === 'asr' && !catalogOpen} onSelectTask={select} />
            <button type="button" className="button button-quiet" disabled={asr.submitting} onClick={() => setLegacySubmission(value => !value)}>{legacySubmission ? '返回当前接口服务提交' : '使用已有 Whisper 实例提交'}</button>
          </details>
          <AsrSchedulingPanel scheduling={scheduling} />
          <div className="work-grid">
            <div className="submit-column" id="audio-test-upload">{legacySubmission && <p className="asr-service-command-notice" role="status">当前使用已有 Whisper 实例提交；可在“已有实例管理与兼容入口”返回接口服务。</p>}<UploadPanel models={submissionModels} modelError={asr.modelError} submitting={asr.submitting} error={asr.submissionError} receipt={asr.submissionReceipt} uncertain={asr.submissionUncertain} serviceBlock={serviceBlock} serviceModel={management.selectedInstance} externalBlock={externalBlock} onSubmit={asr.submit} onReset={asr.clearSubmission} /><WorkflowNote vision={false} /></div>
            <div className="results-column"><TaskList tasks={asr.tasks} error={asr.tasksError} loading={asr.loading} selectedId={asr.selectedTaskId} page={asr.page} onPage={asr.setPage} onSelect={select} /><TaskDetail task={asr.task} result={asr.result} selectedId={asr.selectedTaskId} taskError={asr.taskError} resultError={asr.resultError} loading={asr.selectedLoading} onRefresh={() => void asr.refresh()} onCancel={asr.cancelTask} cancellation={asr.cancellation} /></div>
          </div>
        </div>
        <div hidden={!isVision} data-workspace="vision">
          <div className="work-grid">
            <div className="submit-column"><VisionUploadPanel models={vision.models} modelError={vision.modelError} submitting={vision.submitting} error={vision.submissionError} receipt={vision.submissionReceipt} uncertain={vision.submissionUncertain} onSubmit={vision.submit} onReset={vision.clearSubmission} /><WorkflowNote vision /></div>
            <div className="results-column"><TaskList taskType="vision" tasks={vision.tasks} error={vision.tasksError} loading={vision.loading} selectedId={vision.selectedTaskId} page={vision.page} onPage={vision.setPage} onSelect={select} /><VisionTaskDetail active={isVision} task={vision.task} result={vision.result} selectedId={vision.selectedTaskId} taskError={vision.taskError} resultError={vision.resultError} loading={vision.selectedLoading} onRefresh={() => void vision.refresh()} onCancel={vision.cancelTask} cancellation={vision.cancellation} /></div>
          </div>
        </div>
        <div hidden={!isLlm}><LlmWorkspace platform={llm} /></div>
        <footer className="workspace-footer"><span>{isLlm ? '在线文本通道 · 服务可用性待真实验证' : isVision ? '本地图片检测 · YOLOv5n' : '音频转写 · 手动配置 ASR 接口'}</span><span>{isLlm ? '无服务时不可生成；不会使用模拟回答' : '模型输出中的关键内容请人工复核'}</span></footer>
        </div>
      </main>
    </div>
    <dialog ref={help} className="help-dialog" onClose={() => setHelpOpen(false)} aria-labelledby="help-title">
      <div className="panel-heading"><h2 id="help-title">如何使用推理工作台</h2><button type="button" className="icon-button" aria-label="关闭使用说明" onClick={() => help.current?.close()}><Icon name="close" /></button></div>
      <ol><li><strong>配置并测试服务</strong><p>在“ASR 服务配置”手动填写服务地址、接口协议和模型，点击“测试连接”。保存并选用修订后重启平台；随后使用当前服务上传短音频，确认实际转写。健康检查通过不等于音频测试通过。</p></li><li><strong>选择文件并提交</strong><p>在“音频转写”或“图片检测”中上传文件，按需调整参数。获得任务编号后，后台持续处理，切换工作台不会取消任务。</p></li><li><strong>查看与保存结果</strong><p>音频可查看文字与分段，下载 TXT/JSON；图片可切换原图与检测框图，查看类别、置信度、坐标，下载 JSON 或框图 PNG。结果详情链接可在刷新后恢复。</p></li><li><strong>在线文本生成</strong><p>必须先配置真实大模型服务。文字会随连接逐步显示；停止、切换或隐藏页面会中止连接并尝试通知后台停止。刷新后仅能查询请求状态，正文不保存，不会自动继续或重新生成。</p></li></ol>
      <div className="help-note"><Icon name="info" size={19} /><p>“取消中”表示后台仍在确认停止。本机任务的暂时故障会有限重试。外部 ASR 仅在尚未领取时支持取消，开始调用后不自动重发；超时或断线不代表远端执行已停止。没有检测到目标也是一次成功检测。未确认的上传请用原提交重试；整页刷新会丢失尚未确认提交的文件与编号。</p></div>
      <button type="button" className="button button-primary" onClick={() => help.current?.close()}>开始使用</button>
    </dialog>
  </div>
}

function WorkflowNote({ vision }: { vision: boolean }) {
  return <div className="workflow-note"><h3>一次提交，后台持续处理</h3><div><span>{vision ? '上传图片' : '上传音频'}</span><Icon name="chevron" size={12} /><span>{vision ? '后台检测' : '后台转写'}</span><Icon name="chevron" size={12} /><span>查看结果</span></div><p>切换工作台会保留已选文件。后台按任务编号保存结果，已受理任务不依赖页面保持打开。</p></div>
}

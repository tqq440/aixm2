/** Public camelCase contracts from backend/schema; dates are ISO 8601 UTC strings. */
import type { TaskRoutingView } from '@/types/asrScheduling'
export interface FailureInfo {
  code: string
  message: string
}

export interface ValidationIssue {
  field: string
  code: string
}

export interface SuccessResponse<T> {
  data: T
  requestId: string
}

export interface ErrorResponse {
  error: FailureInfo & { details: ValidationIssue[] }
  requestId: string
}

export type TaskStatus = 'created' | 'queued' | 'running' | 'retry_wait' | 'cancel_requested' | 'succeeded' | 'failed' | 'cancelled'
export type TaskType = 'asr' | 'vision'

/** Only supplied when an external ASR model's upstream version is unknown. */
export interface IntegrationVersionInfo {
  versionBasis: 'integration_revision'
  upstreamVersion: null
}

export interface VisionParameters {
  confidenceThreshold: number
  iouThreshold: number
  maxDetections: number
}

export interface TaskView {
  routing?: TaskRoutingView
  taskId: string
  callerId: string
  taskType: 'asr'
  modelId: string
  modelVersion: string
  versionInfo?: IntegrationVersionInfo
  parameters: { language: string | null }
  status: TaskStatus
  attempt: number
  maxAttempts: number
  executionPlanVersion: number
  input: {
    fileName: string
    sizeBytes: number
    sha256: string
    durationSeconds: number
  }
  createdAt: string
  updatedAt: string
  startedAt: string | null
  finishedAt: string | null
  queueDeadlineAt: string | null
  totalDeadlineAt: string | null
  attemptDeadlineAt: string | null
  nextRetryAt: string | null
  error: FailureInfo | null
  resultAvailable: boolean
  message: string
  cancelAllowed?: boolean
  cancelUnavailableReason?: string | null
  remoteOutcome?: 'not_dispatched' | 'pending' | 'confirmed' | 'unknown' | null
}

export interface VisionTaskView extends Omit<TaskView, 'taskType' | 'parameters' | 'input'> {
  taskType: 'vision'
  parameters: VisionParameters
  input: { fileName: string; sizeBytes: number; sha256: string; mediaType: 'image'; width: number; height: number }
}

export type AnyTaskView = TaskView | VisionTaskView

export interface TaskReceipt<T = TaskView> {
  task: T
  replayed: boolean
}

export interface CancellationReceipt<T = TaskView> {
  task: T
  decision: 'cancelled' | 'cancel_requested' | 'already_finished'
}

export interface TaskPage<T = TaskView> {
  items: T[]
  total: number
  page: number
  pageSize: number
}

export interface ModelSpec {
  modelId: string
  modelVersion: string
  versionInfo?: IntegrationVersionInfo
  modelRepo: string | null
  executionLocation?: 'local' | 'external'
  managementMode?: 'managed' | 'external_call_only'
  taskType: 'asr'
  executionMode: 'offline'
  adapterKey: string
  parameterSchema: {
    defaultLanguage: string
    autoDetectionValue?: string
    languageMode?: 'client' | 'server'
    languages: string[]
  }
  inputConstraints: { maxFileBytes: number; maxAudioSeconds: number }
  outputCapabilities: string[]
}

export interface TranscriptionSegment {
  start: number
  end: number
  text: string
}

export interface VisionModelSpec extends Omit<ModelSpec, 'taskType' | 'parameterSchema' | 'inputConstraints'> {
  taskType: 'vision'
  parameterSchema: {
    defaultConfidenceThreshold: number; defaultIouThreshold: number
    defaultMaxDetections: number; maxDetections: number
  }
  inputConstraints: { maxFileBytes: number; maxPixels: number; formats: string[] }
}

export interface Detection {
  classId: number
  className: string
  confidence: number
  box: { xMin: number; yMin: number; xMax: number; yMax: number }
}

export interface DetectionResult {
  modelId: string; modelVersion: string; modelSha256: string
  imageWidth: number; imageHeight: number
  coordinateSystem: 'original-pixels-exif-normalized'
  detections: Detection[]
  device: 'cpu'; runtimeVersion: string; inputType: 'float16'; preprocessingVersion: string
  loadSeconds: number; inferenceSeconds: number; totalSeconds: number
}

export interface AsrEngineMetadata {
  adapterKey: string
  modelRepo?: string | null
  modelRevision?: string | null
  device?: string | null
  computeType?: string | null
  cpuThreads?: number | null
  modelLoadSeconds?: number | null
  transcriptionSeconds?: number | null
  totalSeconds?: number | null
  runtimeVersions: Record<string, string>
}

export interface TranscriptionResult {
  text: string
  segments: TranscriptionSegment[]
  language: string | null
  languageRequested: string | null
  languageProbability: number | null
  audioDurationSeconds: number
  engineMetadata?: AsrEngineMetadata | null
  // Historical Whisper fields remain readable without becoming requirements
  // for other ASR engines or being invented for a missing measurement.
  modelRepo?: string | null
  modelRevision?: string | null
  device?: string | null
  computeType?: string | null
  cpuThreads?: number | null
  modelLoadSeconds?: number | null
  transcriptionSeconds?: number | null
  totalSeconds?: number | null
  fasterWhisperVersion?: string | null
  ctranslate2Version?: string | null
}

export interface TaskResultView<T = TranscriptionResult> {
  taskId: string
  modelId: string
  modelVersion: string
  versionInfo?: IntegrationVersionInfo
  schemaVersion: '1'
  output: T
}

export type TaskByType<K extends TaskType> = K extends 'vision' ? VisionTaskView : TaskView
export type ModelByType<K extends TaskType> = K extends 'vision' ? VisionModelSpec : ModelSpec
export type ResultByType<K extends TaskType> = K extends 'vision' ? TaskResultView<DetectionResult> : TaskResultView
export type ParametersByType<K extends TaskType> = K extends 'vision' ? VisionParameters : string | null

export interface ExternalAsrStatus {
  configured: boolean
  available: boolean
  state: 'unconfigured' | 'unavailable' | 'ready' | 'running' | 'uncertain' | 'stopping'
  observedAt: string | null
  message: string
  currentTaskId: string | null
  uncertainTaskId: string | null
  modelId: string | null
  modelVersion: string | null
  versionInfo?: IntegrationVersionInfo
  capacity?: ExternalAsrCapacity | null
}

export interface ExternalAsrCapacity {
  observedAt: string
  targetId: string
  registered: boolean
  enabled: boolean
  revisionId: string | null
  capacityGroup: string | null
  deviceKind: 'unknown' | 'cpu' | 'gpu'
  targetLimit: number
  groupLimit: number
  held: number
  uncertain: number
  groupHeld: number
  groupUncertain: number
  remainingCapacity: number
  queued: number
  incompatibleQueued: number
  legacyUnknownCount: number
  poolState: string
  slots: { slotIndex: number; state: string; fresh: boolean; modelReady: boolean; sessionId: string | null; currentTaskId: string | null; lastError: string | null; heartbeatAt: string | null }[]
  activeCalls: { taskId: string; state: 'held' | 'uncertain'; targetId: string; slotIndex: number; ownerFresh: boolean }[]
  activeCallsTotal: number
  activeCallsTruncated: boolean
}

export interface ComponentView {
  componentId: 'worker' | 'vision_worker' | 'dispatcher' | 'remote_asr_worker'
  reportedState: string | null
  availability: 'not_started' | 'online' | 'stale' | 'stopped' | 'error'
  ready: boolean
  modelReady: boolean
  pid: number | null
  childPid: number | null
  activeTaskId: string | null
  heartbeatAt: string | null
  heartbeatAgeSeconds: number | null
  lastError: string | null
}

export interface RuntimeView {
  observedAt: string
  apiStatus: 'running'
  worker: ComponentView
  visionWorker?: ComponentView
  remoteAsrWorker?: ComponentView | null
  dispatcher: ComponentView
  executionConcurrency: number
  externalCallConcurrency?: number
  externalAutomaticRetry?: false
  automaticExecutionRetry: true
  message: string
}

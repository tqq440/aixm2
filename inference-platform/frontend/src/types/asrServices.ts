export interface AsrServiceConfig {
  serviceId: string
  name: string
  protocol: 'remote-v2' | 'openai-audio'
  baseUrl: string
  transcribePath: string
  healthPath: string
  modelId: string
  modelVersion: string | null
  upstreamModel: string | null
  apiKeyEnv: string | null
  caFile: string | null
  insecureTlsOrigin: string | null
  connectTimeoutSeconds: number
  totalTimeoutSeconds: number
}

export interface AsrServiceView extends AsrServiceConfig { revision: string }

export interface AsrServicesState {
  items: AsrServiceView[]
  selectedServiceId: string | null
  selectedRevision: string | null
  runningServiceId: string | null
  runningRevision: string | null
  restartRequired: boolean
  runningModel: { modelId: string; modelVersion: string } | null
  legacyConfiguration: AsrServiceConfig | null
}

export interface AsrServiceTestResult {
  success: boolean
  phase: 'health'
  message: string
  durationMs: number
  configRevision: string
  errorCode: string | null
  apiVersion: string | null
}

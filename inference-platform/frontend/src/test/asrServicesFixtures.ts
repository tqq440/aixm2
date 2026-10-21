import type { AsrServiceView, AsrServicesState } from '@/types/asrServices'

export const serviceFixture = (changes: Partial<AsrServiceView> = {}): AsrServiceView => ({
  serviceId: 'speech-main', name: '语音服务', protocol: 'remote-v2', baseUrl: 'http://127.0.0.1:8013',
  transcribePath: '/api/v1/asr/transcripts', healthPath: '/api/v1/asr/health', modelId: 'speech-model',
  modelVersion: null, upstreamModel: null, apiKeyEnv: null, caFile: null, insecureTlsOrigin: null,
  connectTimeoutSeconds: 5, totalTimeoutSeconds: 600, revision: 'a'.repeat(64), ...changes,
})

export const servicesFixture = (changes: Partial<AsrServicesState> = {}): AsrServicesState => ({
  items: [serviceFixture()], selectedServiceId: 'speech-main', selectedRevision: 'a'.repeat(64),
  runningServiceId: 'speech-main', runningRevision: 'a'.repeat(64), restartRequired: false,
  runningModel: { modelId: 'speech-model', modelVersion: 'integration-fixture' }, legacyConfiguration: null, ...changes,
})

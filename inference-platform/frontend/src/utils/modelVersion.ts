import type { IntegrationVersionInfo } from '../types/api'

export function hasUnknownUpstreamVersion(value: { versionInfo?: IntegrationVersionInfo } | null | undefined): boolean {
  return value?.versionInfo?.versionBasis === 'integration_revision' && value.versionInfo.upstreamVersion === null
}

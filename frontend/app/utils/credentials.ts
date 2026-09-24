import type { AuthType, CredentialStatus } from '@jeanclode/api-types'

type Translate = (key: string) => string

const AUTH_TYPE_KEYS: Record<AuthType, string> = {
  api_key: 'connectors.authType.apiKey',
  jwt: 'connectors.authType.jwt',
  basic_auth: 'connectors.authType.basicAuth',
  oauth2: 'connectors.authType.oauth2',
  none: 'connectors.authType.none',
}

export function authTypeLabel(t: Translate, authType: AuthType): string {
  return t(AUTH_TYPE_KEYS[authType] ?? authType)
}

export function credentialHost(credential: CredentialStatus): string | null {
  const host = credential.settings?.host
  return typeof host === 'string' && host ? host : null
}

/** "API key · api.figma.com" — the host falls back to `fallbackHost` (an MCP server's own). */
export function credentialLabel(t: Translate, credential: CredentialStatus, fallbackHost?: string): string {
  const host = credentialHost(credential) ?? fallbackHost
  const label = authTypeLabel(t, credential.auth_type)
  return host ? `${label} · ${host}` : label
}

/**
 * Provider icon paths and fallback icons.
 * Used by callers of ProviderIcon to resolve a provider name to icon sources.
 */

interface ProviderIconInfo {
  lightSrc?: string
  darkSrc?: string
  fallback: string
}

const PROVIDER_ICONS: Record<string, ProviderIconInfo> = {
  sentry: {
    lightSrc: '/icons/sentry-light.svg',
    darkSrc: '/icons/sentry-dark.svg',
    fallback: 'i-lucide-circle-dot',
  },
  github: {
    fallback: 'i-simple-icons-github',
  },
  gitlab: {
    fallback: 'i-simple-icons-gitlab',
  },
  linear: {
    fallback: 'i-lucide-square-kanban',
  },
}

const DEFAULT_ICON: ProviderIconInfo = { fallback: 'i-lucide-circle-dot' }

export function getProviderIcon(provider: string): ProviderIconInfo {
  return PROVIDER_ICONS[provider] ?? DEFAULT_ICON
}

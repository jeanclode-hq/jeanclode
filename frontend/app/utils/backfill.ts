import type { BackfillScope } from '@jeanclode/api-types'

/**
 * Sentry backfill scope — how much existing history to import when an org
 * is connected. Mirrors `BackfillScope` in the backend settings model.
 *
 * The choice bounds the onboarding import: a large org can carry thousands
 * of unresolved issues, and working through all of them takes days. New
 * errors arrive via webhooks regardless of what is picked here.
 */
export const BACKFILL_SCOPES: readonly BackfillScope[] = ['none', '7d', '30d', 'all']

export const DEFAULT_BACKFILL_SCOPE: BackfillScope = '30d'

const LABEL_KEYS: Record<BackfillScope, string> = {
  'none': 'onboarding.sentry.backfillNone',
  '7d': 'onboarding.sentry.backfill7d',
  '30d': 'onboarding.sentry.backfill30d',
  'all': 'onboarding.sentry.backfillAll',
}

/** Options for a USelect / URadioGroup, labelled through i18n. */
export function backfillOptions(t: (key: string) => string) {
  return BACKFILL_SCOPES.map((value) => ({ value, label: t(LABEL_KEYS[value]) }))
}

/** The one-line explanation shown under the picker for the active scope. */
export function backfillHint(t: (key: string) => string, scope: BackfillScope) {
  return t(`onboarding.sentry.backfillHints.${scope}`)
}

/**
 * Onboarding state composable — tracks onboarding progress per workspace.
 *
 * Two-step flow:
 *   1. git_provider — connect at least one GitHub/GitLab org
 *   2. connectors   — optionally add Sentry, Linear, etc.
 *
 * Persists state in localStorage keyed by workspace ID.
 */
import type { OnboardingStep } from '~/types/api'

const STORAGE_PREFIX = 'jeanclode:onboarding:'

interface PersistedState {
  gitOrgIds: string[]
  sentryOrgIds: string[]
  step: OnboardingStep
}

export function useOnboarding() {
  const workspaceStore = useWorkspaceStore()

  const gitOrgIds = useState<string[]>('onboarding.gitOrgIds', () => [])
  const sentryOrgIds = useState<string[]>('onboarding.sentryOrgIds', () => [])
  const step = useState<OnboardingStep>('onboarding.step', () => 'git_provider')
  const showModal = useState<boolean>('onboarding.showModal', () => false)
  const checked = useState<boolean>('onboarding.checked', () => false)

  // -- Persistence --

  function storageKey(): string | null {
    const id = workspaceStore.currentWorkspace?.id
    return id ? `${STORAGE_PREFIX}${id}` : null
  }

  function load(): boolean {
    if (!import.meta.client) return false
    const key = storageKey()
    if (!key) return false

    try {
      const raw = localStorage.getItem(key)
      if (raw) {
        const saved: PersistedState = JSON.parse(raw)
        gitOrgIds.value = saved.gitOrgIds ?? []
        sentryOrgIds.value = saved.sentryOrgIds ?? []
        step.value = saved.step ?? 'git_provider'
        return true
      }
      return false
    } catch {
      return false
    }
  }

  function save() {
    if (!import.meta.client) return
    const key = storageKey()
    if (!key) return

    const state: PersistedState = {
      gitOrgIds: gitOrgIds.value,
      sentryOrgIds: sentryOrgIds.value,
      step: step.value,
    }
    localStorage.setItem(key, JSON.stringify(state))
  }

  // -- State setters --

  function addGitOrg(id: string) {
    if (!gitOrgIds.value.includes(id)) {
      gitOrgIds.value = [...gitOrgIds.value, id]
      save()
    }
  }

  function removeGitOrg(id: string) {
    gitOrgIds.value = gitOrgIds.value.filter((x) => x !== id)
    save()
  }

  function addSentryOrg(id: string) {
    if (!sentryOrgIds.value.includes(id)) {
      sentryOrgIds.value = [...sentryOrgIds.value, id]
      save()
    }
  }

  function removeSentryOrg(id: string) {
    sentryOrgIds.value = sentryOrgIds.value.filter((x) => x !== id)
    save()
  }

  function setStep(newStep: OnboardingStep) {
    step.value = newStep
    save()
  }

  function complete() {
    step.value = 'complete'
    showModal.value = false
    save()
  }

  function reset() {
    gitOrgIds.value = []
    sentryOrgIds.value = []
    step.value = 'git_provider'
  }

  // -- Computed --

  const needsOnboarding = computed(() => checked.value && step.value !== 'complete')

  const stepperIndex = computed(() => {
    const map: Record<OnboardingStep, number> = {
      git_provider: 0,
      connectors: 1,
      complete: 1,
    }
    return map[step.value] ?? 0
  })

  // -- Lifecycle --

  function check() {
    const hasState = load()
    if (!hasState) {
      // No saved state for this workspace — assume onboarding is done
      // (if it wasn't, the user would have been guided through it on creation)
      step.value = 'complete'
    }
    checked.value = true
    if (hasState && step.value !== 'complete') {
      showModal.value = true
    }
  }

  function start() {
    reset()
    checked.value = true
    showModal.value = true
    save()
  }

  return {
    gitOrgIds: readonly(gitOrgIds),
    sentryOrgIds: readonly(sentryOrgIds),
    step: readonly(step),
    showModal,
    needsOnboarding,
    stepperIndex,
    addGitOrg,
    removeGitOrg,
    addSentryOrg,
    removeSentryOrg,
    setStep,
    complete,
    check,
    start,
    reset,
  }
}

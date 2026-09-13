/**
 * Global watcher that claims a GitHub App installation when
 * `?installation_id=XXX` appears in the URL query params.
 *
 * GitHub redirects to our app after installing the GitHub App.
 * The webhook creates a GitOrg with no workspace — this composable
 * calls the claim endpoint to assign it to the current workspace.
 *
 * The `state` query param controls where to navigate after claim:
 * - "integrations" → /integrations
 * - "onboarding" or absent → / (onboarding modal auto-opens)
 *
 * Called once in the default layout.
 */
export function useGitHubClaim() {
  const route = useRoute()
  const router = useRouter()
  const toast = useToast()
  const workspaceStore = useWorkspaceStore()
  const claimMutation = useClaimOrgMutation()
  const claiming = ref(false)

  const pendingInstallationId = computed(() => {
    const val = route.query.installation_id
    return typeof val === 'string' ? val : null
  })

  async function claimWithRetry(installationId: string, workspaceId: string, maxRetries = 10) {
    claiming.value = true
    for (let i = 0; i < maxRetries; i++) {
      try {
        await claimMutation.mutateAsync({ installationId, workspaceId })
        // Navigate based on state param
        const redirectTo = route.query.state === 'integrations' ? '/integrations' : '/'
        router.replace({ path: redirectTo, query: {} })
        claiming.value = false
        return
      } catch {
        await new Promise((r) => setTimeout(r, 1000))
      }
    }
    toast.add({ title: 'Claim failed', description: 'Could not claim GitHub installation after retries', color: 'error' })
    claiming.value = false
  }

  watch(
    [pendingInstallationId, () => workspaceStore.currentWorkspace?.id],
    ([installationId, wsId]) => {
      if (installationId && wsId && !claiming.value) {
        claimWithRetry(installationId, wsId)
      }
    },
    { immediate: true },
  )

  return { claiming }
}

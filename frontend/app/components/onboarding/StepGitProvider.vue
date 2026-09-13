<script setup lang="ts">
import { listOrganizations } from '@jeanclode/api-types'
import type { OrgResponse, RepoResponse } from '@jeanclode/api-types'

const props = defineProps<{
  workspaceId: string
}>()

const emit = defineEmits<{
  continue: []
}>()

const client = useApi()
const { user: authUser } = useAuth()
const onboarding = useOnboarding()
const { data: appConfig } = useAppConfigQuery()
const toast = useToast()

// UI state
const selectedProvider = ref<'github' | 'gitlab' | null>(null)
const gitlabToken = ref('')
const manageProjectWebhooks = ref(false)
const defaultGitlabUrl = computed(() => appConfig.value?.providers.gitlab_instance_url ?? 'https://gitlab.com')
const gitlabUrl = ref(defaultGitlabUrl.value)
const waitingForGitHub = ref(false)
const claimingGitHub = ref(false)

// Reset the URL field to the configured instance whenever the GitLab form
// is (re)opened — appConfig may not have resolved yet at ref-init time.
watch(selectedProvider, (provider) => {
  if (provider === 'gitlab') gitlabUrl.value = defaultGitlabUrl.value
})

// Mutations
const gitLabMutation = useAddGitLabSourceMutation()
const deleteMutation = useDeleteOrgMutation()
const isConnectingGitLab = gitLabMutation.isLoading
const deletingOrgId = ref<string | null>(null)

// Provider availability from backend config
const githubEnabled = computed(() => appConfig.value?.providers.github_enabled ?? false)
const gitlabEnabled = computed(() => appConfig.value?.providers.gitlab_enabled ?? false)
const githubInstallUrl = computed(() => appConfig.value?.providers.github_app_install_url ?? '')

// Check if user has linked the provider (needed for GitHub App install)
const hasGithubIdentity = computed(() => !!authUser.value?.github_external_id)
const hasGitlabIdentity = computed(() => !!authUser.value?.gitlab_external_id)

const config = useRuntimeConfig()
function linkProvider(provider: 'github' | 'gitlab') {
  window.location.href = `${config.public.apiBase}/auth/link/${provider}`
}

// Poll for git orgs in the workspace (discovers GitHub-installed orgs)
const { data: workspaceOrgs, refresh: refreshOrgs } = useQuery({
  key: () => ['organizations', 'workspace', props.workspaceId],
  query: async () => {
    const { data } = await listOrganizations({
      client,
      query: { workspace_id: props.workspaceId, provider: ['github', 'gitlab'] },
    })
    return data ?? []
  },
  enabled: () => !!props.workspaceId,
  refetchOnWindowFocus: true,
})

// Fetch repos for each org
const connectedOrgs = ref<(OrgResponse & { repos: RepoResponse[] })[]>([])
const loadingOrgs = ref(false)

async function fetchOrgDetails() {
  const orgs = workspaceOrgs.value
  if (!orgs || orgs.length === 0) {
    connectedOrgs.value = []
    return
  }

  loadingOrgs.value = true
  try {
    const detailed = await Promise.all(
      orgs.map(async (org) => {
        const repos = await fetchAllOrgRepos(client, org.id)
        return { ...org, repos }
      }),
    )
    connectedOrgs.value = detailed

    // Auto-track any orgs we discover (e.g. GitHub webhook-created)
    for (const org of detailed) {
      onboarding.addGitOrg(org.id)
    }

    // If we were waiting for GitHub, stop
    if (waitingForGitHub.value && detailed.some((o) => o.provider === 'github')) {
      waitingForGitHub.value = false
      selectedProvider.value = null
    }
  } catch {
    // Ignore
  } finally {
    loadingOrgs.value = false
  }
}

watch(workspaceOrgs, fetchOrgDetails, { immediate: true })

// SSE sync events invalidate the git-orgs and repos cache automatically
// (handled by useEventStream in the layout), so no polling needed.

// Claim is handled globally by useGitHubClaim() in the layout.
// When claim succeeds, the git-orgs cache is invalidated and workspaceOrgs refreshes.
// We watch for new orgs to track them in onboarding state.
watch(workspaceOrgs, (orgs) => {
  if (!orgs) return
  for (const org of orgs) {
    onboarding.addGitOrg(org.id)
  }
  if (orgs.length > 0) {
    claimingGitHub.value = false
  }
})

function handleGitHubInstall() {
  waitingForGitHub.value = true
  const url = new URL(githubInstallUrl.value)
  url.searchParams.set('state', 'onboarding')
  window.location.href = url.toString()
}

async function handleGitLabConnect() {
  if (!gitlabToken.value.trim()) return
  try {
    const result = await gitLabMutation.mutateAsync({
      accessToken: gitlabToken.value.trim(),
      gitlabUrl: gitlabUrl.value,
      workspaceId: props.workspaceId,
      manageProjectWebhooks: manageProjectWebhooks.value,
    })
    onboarding.addGitOrg(result.source_id)
    gitlabToken.value = ''
    gitlabUrl.value = defaultGitlabUrl.value
    manageProjectWebhooks.value = false
    selectedProvider.value = null
    refreshOrgs()
  } catch (e: unknown) {
    toast.add({ title: 'Connection failed', description: extractApiError(e, 'Failed to connect GitLab'), color: 'error' })
  }
}

async function handleDeleteOrg(orgId: string) {
  deletingOrgId.value = orgId
  try {
    await deleteMutation.mutateAsync(orgId)
    onboarding.removeGitOrg(orgId)
    refreshOrgs()
  } catch {
    toast.add({ title: 'Delete failed', description: 'Failed to remove the git provider', color: 'error' })
  } finally {
    deletingOrgId.value = null
  }
}

const hasProviders = computed(() => connectedOrgs.value.length > 0)
</script>

<template>
  <div class="flex flex-col gap-5">
    <!-- Loading -->
    <div
      v-if="loadingOrgs && connectedOrgs.length === 0"
      class="flex items-center justify-center py-12"
    >
      <UIcon
        name="i-lucide-loader-2"
        class="size-5 text-neutral-400 animate-spin"
      />
    </div>

    <!-- Connected providers -->
    <div
      v-if="hasProviders"
      class="space-y-3"
    >
      <p class="text-xs font-medium text-neutral-500 dark:text-neutral-400 uppercase tracking-wider">
        Connected providers
      </p>
      <div
        v-for="org in connectedOrgs"
        :key="org.id"
        class="group relative rounded-xl border border-neutral-200 dark:border-neutral-600 bg-white dark:bg-neutral-900 overflow-hidden"
      >
        <!-- Header -->
        <div class="flex items-center gap-4 p-4">
          <div
            class="size-10 rounded-lg flex items-center justify-center shrink-0"
            :class="org.provider === 'github'
              ? 'bg-neutral-900 dark:bg-white'
              : 'bg-orange-50 dark:bg-orange-950'"
          >
            <UIcon
              :name="org.provider === 'github' ? 'i-simple-icons-github' : 'i-simple-icons-gitlab'"
              class="size-5"
              :class="org.provider === 'github'
                ? 'text-white dark:text-neutral-900'
                : 'text-orange-500'"
            />
          </div>

          <div class="flex-1 min-w-0">
            <p class="text-sm font-semibold text-neutral-900 dark:text-neutral-100 truncate">
              {{ org.name }}
            </p>
            <p class="text-xs text-neutral-500 dark:text-neutral-400 mt-0.5">
              {{ org.provider === 'github' ? 'GitHub' : 'GitLab' }}
              <span v-if="org.base_url && org.base_url !== 'https://gitlab.com' && org.base_url !== 'https://github.com'">
                &middot; {{ org.base_url }}
              </span>
              <template v-if="org.repos.length > 0">
                &middot; {{ org.repos.length }} {{ org.repos.length === 1 ? 'repository' : 'repositories' }}
              </template>
              <span
                v-else
                class="inline-flex items-center gap-1"
              >
                &middot;
                <UIcon
                  name="i-lucide-loader-2"
                  class="size-3 animate-spin"
                />
                Syncing repositories
              </span>
            </p>
          </div>

          <div class="flex items-center gap-2 shrink-0">
            <span class="size-2 rounded-full bg-green-500" />
            <UButton
              icon="i-lucide-trash-2"
              variant="ghost"
              color="neutral"
              size="xs"
              :loading="deletingOrgId === org.id"
              @click="handleDeleteOrg(org.id)"
            />
          </div>
        </div>

        <!-- Repos list -->
        <div
          v-if="org.repos.length > 0"
          class="border-t border-neutral-100 dark:border-neutral-700 px-4 py-2 max-h-32 overflow-y-auto"
        >
          <div
            v-for="repo in org.repos"
            :key="repo.id"
            class="flex items-center gap-2 py-1"
          >
            <UIcon
              name="i-lucide-book"
              class="size-3.5 text-neutral-400 shrink-0"
            />
            <span class="text-xs text-neutral-600 dark:text-neutral-400 truncate">
              {{ repo.name }}
            </span>
          </div>
        </div>
      </div>
    </div>

    <!-- Provider cards (always visible) -->
    <div
      v-if="!selectedProvider"
      class="space-y-3"
    >
      <p class="text-xs font-medium text-neutral-500 dark:text-neutral-400 uppercase tracking-wider">
        {{ hasProviders ? 'Add another provider' : 'Connect a provider' }}
      </p>

      <div
        class="grid gap-3"
        :class="githubEnabled && gitlabEnabled ? 'grid-cols-1 sm:grid-cols-2' : 'grid-cols-1 max-w-xs mx-auto'"
      >
        <!-- GitHub card -->
        <button
          v-if="githubEnabled"
          type="button"
          class="cursor-pointer group/card flex items-center gap-3 p-4 rounded-xl border border-neutral-200 dark:border-neutral-600 hover:border-neutral-400 dark:hover:border-neutral-500 hover:bg-neutral-50 dark:hover:bg-neutral-800/60 transition-all"
          @click="selectedProvider = 'github'"
        >
          <div class="size-10 rounded-lg bg-neutral-900 dark:bg-white flex items-center justify-center shrink-0">
            <UIcon
              name="i-simple-icons-github"
              class="size-5 text-white dark:text-neutral-900"
            />
          </div>
          <div class="text-left">
            <p class="text-sm font-medium text-neutral-900 dark:text-neutral-100">
              GitHub
            </p>
            <p class="text-[11px] text-neutral-500 dark:text-neutral-400">
              Install the GitHub App
            </p>
          </div>
          <UIcon
            name="i-lucide-chevron-right"
            class="size-4 text-neutral-300 dark:text-neutral-600 ml-auto shrink-0 group-hover/card:text-neutral-700 dark:group-hover/card:text-neutral-200 transition-colors"
          />
        </button>

        <!-- GitLab card -->
        <button
          v-if="gitlabEnabled"
          type="button"
          class="cursor-pointer group/card flex items-center gap-3 p-4 rounded-xl border border-neutral-200 dark:border-neutral-600 hover:border-neutral-400 dark:hover:border-neutral-500 hover:bg-neutral-50 dark:hover:bg-neutral-800/60 transition-all"
          @click="selectedProvider = 'gitlab'"
        >
          <div class="size-10 rounded-lg bg-orange-50 dark:bg-orange-950 flex items-center justify-center shrink-0">
            <UIcon
              name="i-simple-icons-gitlab"
              class="size-5 text-orange-500"
            />
          </div>
          <div class="text-left">
            <p class="text-sm font-medium text-neutral-900 dark:text-neutral-100">
              GitLab
            </p>
            <p class="text-[11px] text-neutral-500 dark:text-neutral-400">
              Paste an access token
            </p>
          </div>
          <UIcon
            name="i-lucide-chevron-right"
            class="size-4 text-neutral-300 dark:text-neutral-600 ml-auto shrink-0 group-hover/card:text-neutral-700 dark:group-hover/card:text-neutral-200 transition-colors"
          />
        </button>
      </div>
    </div>

    <!-- GitHub expanded flow -->
    <div
      v-if="selectedProvider === 'github'"
      class="rounded-xl border border-neutral-200 dark:border-neutral-600 p-6 text-center"
    >
      <div class="size-12 rounded-lg bg-neutral-900 dark:bg-white flex items-center justify-center mx-auto mb-4">
        <UIcon
          name="i-simple-icons-github"
          class="size-6 text-white dark:text-neutral-900"
        />
      </div>

      <!-- Has GitHub identity → install app -->
      <template v-if="hasGithubIdentity">
        <p class="text-sm font-medium text-neutral-900 dark:text-neutral-100 mb-1">
          Install the GitHub App
        </p>
        <p class="text-xs text-neutral-500 dark:text-neutral-400 mb-5 max-w-xs mx-auto">
          You'll be taken to GitHub to select an organization and grant repository access. You'll be redirected back here when done.
        </p>
        <div class="flex items-center justify-center gap-3">
          <UButton
            label="Cancel"
            variant="ghost"
            color="neutral"
            size="sm"
            @click="selectedProvider = null"
          />
          <UButton
            label="Continue to GitHub"
            icon="i-lucide-external-link"
            trailing
            @click="handleGitHubInstall"
          />
        </div>
      </template>

      <!-- No GitHub identity → link account first -->
      <template v-else>
        <p class="text-sm font-medium text-neutral-900 dark:text-neutral-100 mb-1">
          Link your GitHub account
        </p>
        <p class="text-xs text-neutral-500 dark:text-neutral-400 mb-5 max-w-xs mx-auto">
          You need to connect your GitHub account before you can install the app on an organization.
        </p>
        <div class="flex items-center justify-center gap-3">
          <UButton
            label="Cancel"
            variant="ghost"
            color="neutral"
            size="sm"
            @click="selectedProvider = null"
          />
          <UButton
            label="Link GitHub"
            icon="i-lucide-link"
            @click="linkProvider('github')"
          />
        </div>
      </template>
    </div>

    <!-- GitLab expanded flow -->
    <div
      v-if="selectedProvider === 'gitlab'"
      class="rounded-xl border border-neutral-200 dark:border-neutral-600 overflow-hidden"
    >
      <!-- Has GitLab identity → token form -->
      <form
        v-if="hasGitlabIdentity"
        class="p-5 space-y-4"
        @submit.prevent="handleGitLabConnect"
      >
        <div class="flex items-center gap-3 mb-2">
          <div class="size-8 rounded-lg bg-orange-50 dark:bg-orange-950 flex items-center justify-center shrink-0">
            <UIcon
              name="i-simple-icons-gitlab"
              class="size-4 text-orange-500"
            />
          </div>
          <div>
            <p class="text-sm font-medium text-neutral-900 dark:text-neutral-100">
              Connect GitLab
            </p>
            <p class="text-[11px] text-neutral-500 dark:text-neutral-400">
              Provide a group or project access token
            </p>
          </div>
        </div>

        <UFormField required>
          <template #label>
            <span class="inline-flex items-center gap-1.5">
              {{ $t('onboarding.gitProvider.gitlab.tokenLabel') }}
              <UTooltip :text="$t('onboarding.gitProvider.gitlab.tokenHint')">
                <UIcon
                  name="i-lucide-info"
                  class="size-3.5 text-neutral-400 hover:text-neutral-600 dark:hover:text-neutral-300 cursor-help transition-colors"
                />
              </UTooltip>
            </span>
          </template>
          <UInput
            v-model="gitlabToken"
            type="password"
            :placeholder="$t('onboarding.gitProvider.gitlab.tokenPlaceholder')"
            autofocus
            class="w-full"
          />
        </UFormField>

        <UFormField :label="$t('onboarding.gitProvider.gitlab.urlLabel')">
          <UInput
            v-model="gitlabUrl"
            :placeholder="$t('onboarding.gitProvider.gitlab.urlPlaceholder')"
            class="w-full"
          />
        </UFormField>

        <div class="flex items-start justify-between gap-3">
          <div class="min-w-0">
            <p class="text-sm text-neutral-900 dark:text-neutral-100">
              {{ $t('onboarding.gitProvider.gitlab.manageProjectWebhooksLabel') }}
            </p>
            <p class="text-[11px] text-neutral-500 dark:text-neutral-400 mt-0.5">
              {{ $t('onboarding.gitProvider.gitlab.manageProjectWebhooksHint') }}
            </p>
          </div>
          <USwitch
            v-model="manageProjectWebhooks"
            size="sm"
            class="mt-0.5 shrink-0"
          />
        </div>

        <div class="flex items-center justify-end gap-2 pt-1">
          <UButton
            label="Cancel"
            variant="ghost"
            color="neutral"
            size="sm"
            @click="selectedProvider = null"
          />
          <UButton
            type="submit"
            label="Connect"
            :loading="isConnectingGitLab"
            :disabled="!gitlabToken.trim()"
          />
        </div>
      </form>

      <!-- No GitLab identity → link account first -->
      <div
        v-else
        class="p-6 text-center"
      >
        <div class="size-12 rounded-lg bg-orange-50 dark:bg-orange-950 flex items-center justify-center mx-auto mb-4">
          <UIcon
            name="i-simple-icons-gitlab"
            class="size-6 text-orange-500"
          />
        </div>
        <p class="text-sm font-medium text-neutral-900 dark:text-neutral-100 mb-1">
          Link your GitLab account
        </p>
        <p class="text-xs text-neutral-500 dark:text-neutral-400 mb-5 max-w-xs mx-auto">
          You need to connect your GitLab account before you can add organizations.
        </p>
        <div class="flex items-center justify-center gap-3">
          <UButton
            label="Cancel"
            variant="ghost"
            color="neutral"
            size="sm"
            @click="selectedProvider = null"
          />
          <UButton
            label="Link GitLab"
            icon="i-lucide-link"
            @click="linkProvider('gitlab')"
          />
        </div>
      </div>
    </div>

    <!-- Continue button -->
    <div
      v-if="hasProviders && !selectedProvider"
      class="flex justify-end pt-3 border-t border-neutral-100 dark:border-neutral-700"
    >
      <UButton
        :label="$t('common.continue')"
        icon="i-lucide-arrow-right"
        trailing
        @click="emit('continue')"
      />
    </div>
  </div>
</template>

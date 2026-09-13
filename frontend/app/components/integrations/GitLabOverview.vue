<script setup lang="ts">
import type { OrgResponse } from '@jeanclode/api-types'

const props = defineProps<{
  orgs: OrgResponse[]
  workspaceId: string
}>()

const emit = defineEmits<{
  select: [orgId: string]
  refresh: []
}>()

const { hasProvider, linkProvider } = useAuth()
const toast = useToast()
const { data: appConfig } = useAppConfigQuery()

const hasGitlabIdentity = computed(() => hasProvider('gitlab'))

// Connect form
const showConnectForm = ref(false)
const gitlabToken = ref('')
const manageProjectWebhooks = ref(false)
const defaultGitlabUrl = computed(() => appConfig.value?.providers.gitlab_instance_url ?? 'https://gitlab.com')
const gitlabUrl = ref(defaultGitlabUrl.value)
const gitLabMutation = useAddGitLabSourceMutation()
const isConnecting = gitLabMutation.isLoading

// Reset the URL field to the configured instance whenever the form reopens
// — appConfig may not have resolved yet at ref-init time.
watch(showConnectForm, (open) => {
  if (open) gitlabUrl.value = defaultGitlabUrl.value
})

async function handleConnect() {
  if (!gitlabToken.value.trim()) return
  try {
    await gitLabMutation.mutateAsync({
      accessToken: gitlabToken.value.trim(),
      gitlabUrl: gitlabUrl.value,
      workspaceId: props.workspaceId,
      manageProjectWebhooks: manageProjectWebhooks.value,
    })
    gitlabToken.value = ''
    gitlabUrl.value = defaultGitlabUrl.value
    manageProjectWebhooks.value = false
    showConnectForm.value = false
    emit('refresh')
  } catch (e: unknown) {
    toast.add({ title: 'Connection failed', description: extractApiError(e, 'Failed to connect GitLab'), color: 'error' })
  }
}

function timeAgo(dateStr: string) {
  const diff = Date.now() - new Date(dateStr).getTime()
  const mins = Math.floor(diff / 60000)
  if (mins < 60) return `${mins}m ago`
  const hrs = Math.floor(mins / 60)
  if (hrs < 24) return `${hrs}h ago`
  const days = Math.floor(hrs / 24)
  return `${days}d ago`
}
</script>

<template>
  <div>
    <!-- Empty state -->
    <div
      v-if="orgs.length === 0 && !showConnectForm"
      class="flex flex-col items-center justify-center py-20 text-center animate-fade-up"
    >
      <div class="size-16 rounded-2xl bg-neutral-100 dark:bg-neutral-800 flex items-center justify-center mb-5">
        <UIcon
          name="i-simple-icons-gitlab"
          class="size-8 text-neutral-500"
        />
      </div>
      <h3 class="text-base font-semibold text-neutral-900 dark:text-neutral-100 mb-1.5">
        Connect a GitLab group
      </h3>
      <p class="text-sm text-neutral-500 dark:text-neutral-400 max-w-sm mb-6">
        Provide a group or project access token to sync repositories from your GitLab instance.
      </p>
      <UButton
        v-if="hasGitlabIdentity"
        label="Add access token"
        icon="i-lucide-key"
        size="lg"
        @click="showConnectForm = true"
      />
      <div v-else>
        <p class="text-xs text-neutral-400 mb-3">
          Link your GitLab account first.
        </p>
        <UButton
          label="Link GitLab Account"
          icon="i-lucide-plug"
          @click="linkProvider('gitlab')"
        />
      </div>
    </div>

    <!-- Connected orgs + optional form -->
    <div
      v-else
      class="space-y-4"
    >
      <div
        v-if="orgs.length > 0"
        class="flex items-center justify-between"
      >
        <p class="text-xs font-medium text-neutral-500 dark:text-neutral-400 uppercase tracking-wider">
          {{ orgs.length }} group{{ orgs.length !== 1 ? 's' : '' }} connected
        </p>
        <UButton
          v-if="hasGitlabIdentity"
          label="Add group"
          icon="i-lucide-plus"
          variant="ghost"
          color="neutral"
          size="xs"
          @click="showConnectForm = !showConnectForm"
        />
      </div>

      <!-- Connect form -->
      <div
        v-if="showConnectForm"
        class="rounded-xl border border-neutral-200 dark:border-neutral-700 bg-neutral-50 dark:bg-neutral-900/50 p-5"
      >
        <form
          class="space-y-4"
          @submit.prevent="handleConnect"
        >
          <p class="text-sm font-medium text-neutral-900 dark:text-neutral-100">
            Add GitLab group
          </p>
          <UFormField required>
            <template #label>
              <span class="inline-flex items-center gap-1.5">
                {{ $t('integrations.gitlab.tokenLabel') }}
                <UTooltip :text="$t('integrations.gitlab.tokenHint')">
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
              :placeholder="$t('integrations.gitlab.tokenPlaceholder')"
              autofocus
              class="w-full"
            />
          </UFormField>
          <UFormField :label="$t('integrations.gitlab.urlLabel')">
            <UInput
              v-model="gitlabUrl"
              :placeholder="$t('integrations.gitlab.urlPlaceholder')"
              class="w-full"
            />
          </UFormField>
          <div class="flex items-start justify-between gap-3">
            <div class="min-w-0">
              <p class="text-sm text-neutral-900 dark:text-neutral-100">
                {{ $t('integrations.gitlab.manageProjectWebhooksLabel') }}
              </p>
              <p class="text-[11px] text-neutral-500 dark:text-neutral-400 mt-0.5">
                {{ $t('integrations.gitlab.manageProjectWebhooksHint') }}
              </p>
            </div>
            <USwitch
              v-model="manageProjectWebhooks"
              size="sm"
              class="mt-0.5 shrink-0"
            />
          </div>
          <div class="flex items-center justify-end gap-2">
            <UButton
              :label="$t('common.cancel')"
              variant="ghost"
              color="neutral"
              size="sm"
              @click="showConnectForm = false"
            />
            <UButton
              type="submit"
              label="Connect"
              size="sm"
              :loading="isConnecting"
              :disabled="!gitlabToken.trim()"
            />
          </div>
        </form>
      </div>

      <!-- Org rows -->
      <div
        v-if="orgs.length > 0"
        class="rounded-xl border border-neutral-200 dark:border-neutral-700 divide-y divide-neutral-100 dark:divide-neutral-800 overflow-hidden animate-fade-up"
      >
        <button
          v-for="org in orgs"
          :key="org.id"
          type="button"
          class="cursor-pointer w-full flex items-center gap-4 px-4 py-3.5 text-left hover:bg-neutral-50 dark:hover:bg-neutral-800/50 transition-colors group"
          @click="emit('select', org.id)"
        >
          <div class="size-9 rounded-lg bg-neutral-100 dark:bg-neutral-800 flex items-center justify-center shrink-0">
            <UIcon
              name="i-simple-icons-gitlab"
              class="size-4.5 text-neutral-500"
            />
          </div>
          <div class="flex-1 min-w-0">
            <p class="text-sm font-medium text-neutral-900 dark:text-neutral-100 truncate">
              {{ org.name }}
            </p>
            <p class="text-xs text-neutral-400 mt-0.5 truncate">
              {{ org.repo_count }} {{ org.repo_count === 1 ? 'repo' : 'repos' }}
              <span class="hidden sm:inline">
                <template v-if="org.base_url && org.base_url !== 'https://gitlab.com'">&middot; {{ org.base_url }}</template>
                &middot; Added {{ timeAgo(org.created_at) }}
              </span>
            </p>
          </div>
          <div class="flex items-center gap-2.5 shrink-0">
            <UBadge
              label="Connected"
              color="success"
              variant="subtle"
              size="xs"
            />
            <UIcon
              name="i-lucide-chevron-right"
              class="size-4 text-neutral-300 dark:text-neutral-600 group-hover:text-neutral-500 dark:group-hover:text-neutral-400 transition-colors"
            />
          </div>
        </button>
      </div>
    </div>
  </div>
</template>

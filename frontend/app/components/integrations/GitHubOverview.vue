<script setup lang="ts">
import type { OrgResponse } from '@jeanclode/api-types'

defineProps<{
  orgs: OrgResponse[]
  workspaceId: string
}>()

const emit = defineEmits<{
  select: [orgId: string]
  refresh: []
}>()

const { hasProvider, linkProvider } = useAuth()
const { data: appConfig } = useAppConfigQuery()

const githubInstallBaseUrl = computed(() => appConfig.value?.providers.github_app_install_url ?? '')
const hasGithubIdentity = computed(() => hasProvider('github'))

function handleInstall() {
  const url = new URL(githubInstallBaseUrl.value)
  url.searchParams.set('state', 'integrations')
  window.location.href = url.toString()
}

// Claim is handled globally by useGitHubClaim() in the default layout
const { claiming: claimingGitHub } = useGitHubClaim()

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
      v-if="orgs.length === 0 && !claimingGitHub"
      class="flex flex-col items-center justify-center py-20 text-center animate-fade-up"
    >
      <div class="size-16 rounded-2xl bg-neutral-100 dark:bg-neutral-800 flex items-center justify-center mb-5">
        <UIcon
          name="i-simple-icons-github"
          class="size-8 text-neutral-500"
        />
      </div>
      <h3 class="text-base font-semibold text-neutral-900 dark:text-neutral-100 mb-1.5">
        Connect a GitHub organization
      </h3>
      <p class="text-sm text-neutral-500 dark:text-neutral-400 max-w-sm mb-6">
        Install the Jeanclode GitHub App to sync repositories and enable automated fixes for your codebase.
      </p>
      <UButton
        v-if="hasGithubIdentity"
        label="Install GitHub App"
        icon="i-simple-icons-github"
        size="lg"
        @click="handleInstall"
      />
      <div v-else>
        <p class="text-xs text-neutral-400 mb-3">
          Link your GitHub account first to install the app.
        </p>
        <UButton
          label="Link GitHub Account"
          icon="i-lucide-plug"
          @click="linkProvider('github')"
        />
      </div>
    </div>

    <!-- Claiming spinner -->
    <div
      v-else-if="claimingGitHub"
      class="flex flex-col items-center justify-center py-20 text-center"
    >
      <UIcon
        name="i-lucide-loader-2"
        class="size-8 text-neutral-500 animate-spin mb-4"
      />
      <p class="text-sm font-medium text-neutral-700 dark:text-neutral-300">
        Connecting GitHub organization...
      </p>
      <p class="text-xs text-neutral-400 mt-1">
        This may take a few seconds while we sync.
      </p>
    </div>

    <!-- Connected orgs -->
    <div
      v-else
      class="space-y-4"
    >
      <div class="flex items-center justify-between">
        <p class="text-xs font-medium text-neutral-500 dark:text-neutral-400 uppercase tracking-wider">
          {{ orgs.length }} organization{{ orgs.length !== 1 ? 's' : '' }} connected
        </p>
        <UButton
          v-if="hasGithubIdentity"
          label="Add organization"
          icon="i-lucide-plus"
          variant="ghost"
          color="neutral"
          size="xs"
          @click="handleInstall"
        />
      </div>

      <div class="rounded-xl border border-neutral-200 dark:border-neutral-700 divide-y divide-neutral-100 dark:divide-neutral-800 overflow-hidden animate-fade-up">
        <button
          v-for="org in orgs"
          :key="org.id"
          type="button"
          class="cursor-pointer w-full flex items-center gap-4 px-4 py-3.5 text-left hover:bg-neutral-50 dark:hover:bg-neutral-800/50 transition-colors group"
          @click="emit('select', org.id)"
        >
          <UAvatar
            :src="org.avatar_url ?? undefined"
            :text="org.name.charAt(0).toUpperCase()"
            size="md"
          />
          <div class="flex-1 min-w-0">
            <p class="text-sm font-medium text-neutral-900 dark:text-neutral-100 truncate">
              {{ org.name }}
            </p>
            <p class="text-xs text-neutral-400 mt-0.5 truncate">
              {{ org.repo_count }} {{ org.repo_count === 1 ? 'repo' : 'repos' }}
              <span class="hidden sm:inline">&middot; Added {{ timeAgo(org.created_at) }}</span>
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

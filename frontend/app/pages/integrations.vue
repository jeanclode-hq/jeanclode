<script setup lang="ts">
const { t } = useI18n()
const workspaceStore = useWorkspaceStore()
const { data: appConfig } = useAppConfigQuery()

useHead({
  title: () => `${t('integrations.title')} - Jeanclode`,
})

// Current workspace
const workspaceId = computed(() => workspaceStore.currentWorkspace?.id ?? '')

// Queries — single unified query, filtered by provider in computed
const { data: allOrgs, refresh: refreshOrgs } = useOrgsQuery(workspaceId)

// Provider availability
const githubEnabled = computed(() => appConfig.value?.providers.github_enabled ?? false)
const gitlabEnabled = computed(() => appConfig.value?.providers.gitlab_enabled ?? false)

// Sidebar tabs
type IntegrationType = 'github' | 'gitlab' | 'sentry'
const route = useRoute()
const initialTab = (['github', 'gitlab', 'sentry'] as const).includes(route.query.tab as IntegrationType)
  ? (route.query.tab as IntegrationType)
  : 'github'
const requestedIntegration = ref<IntegrationType>(initialTab)

const githubOrgs = computed(() => (allOrgs.value ?? []).filter((o) => o.provider === 'github'))
const gitlabOrgs = computed(() => (allOrgs.value ?? []).filter((o) => o.provider === 'gitlab'))
const sentryOrgs = computed(() => (allOrgs.value ?? []).filter((o) => o.provider === 'sentry'))

const tabs = computed(() => [
  {
    id: 'github' as const,
    label: t('integrations.github.title'),
    icon: 'i-simple-icons-github',
    connected: githubOrgs.value.length > 0,
    disabled: !githubEnabled.value,
  },
  {
    id: 'gitlab' as const,
    label: t('integrations.gitlab.title'),
    icon: 'i-simple-icons-gitlab',
    connected: gitlabOrgs.value.length > 0,
    disabled: !gitlabEnabled.value,
  },
  {
    id: 'sentry' as const,
    label: t('integrations.sentry.title'),
    icon: null,
    connected: sentryOrgs.value.length > 0,
    disabled: false,
  },
])

// A provider the instance doesn't have enabled must never own the content
// area — its tab is unclickable, so a stale ?tab= or the 'github' default
// would otherwise strand the user on an empty state they can't act on.
const activeIntegration = computed<IntegrationType>(() => {
  const requested = tabs.value.find((tab) => tab.id === requestedIntegration.value)
  if (requested && !requested.disabled) return requested.id
  return tabs.value.find((tab) => !tab.disabled)?.id ?? 'sentry'
})

// Detail drill-down
const selectedOrgId = ref<string | null>((route.query.org as string) || null)

const selectedGitOrg = computed(() =>
  selectedOrgId.value && activeIntegration.value !== 'sentry'
    ? (allOrgs.value ?? []).find((o) => o.id === selectedOrgId.value) ?? null
    : null,
)
const selectedSentryOrg = computed(() =>
  selectedOrgId.value && activeIntegration.value === 'sentry'
    ? (allOrgs.value ?? []).find((o) => o.id === selectedOrgId.value) ?? null
    : null,
)

function selectIntegration(id: IntegrationType) {
  requestedIntegration.value = id
  selectedOrgId.value = null
}

function handleOrgClick(orgId: string) {
  selectedOrgId.value = orgId
}

function handleBack() {
  selectedOrgId.value = null
}

function handleRefresh() {
  refreshOrgs()
}
</script>

<template>
  <div class="flex flex-col lg:flex-row h-full px-4 lg:px-0">
    <!-- Integration tabs -->
    <PageNav
      :items="tabs.map(t => ({ id: t.id, label: t.label, icon: t.icon ?? undefined, disabled: t.disabled, dot: t.connected }))"
      :model-value="activeIntegration"
      @update:model-value="selectIntegration($event as IntegrationType)"
    >
      <template #sentry-icon>
        <ProviderIcon
          light-src="/icons/sentry-light.svg"
          dark-src="/icons/sentry-dark.svg"
          alt="Sentry"
          class="size-4 shrink-0"
        />
      </template>
    </PageNav>

    <!-- Content Area -->
    <div class="flex-1 overflow-y-auto pr-4 lg:pl-8">
      <Transition
        mode="out-in"
        enter-active-class="transition-all duration-200 ease-out"
        enter-from-class="opacity-0 translate-y-1"
        enter-to-class="opacity-100 translate-y-0"
        leave-active-class="transition-all duration-150 ease-in"
        leave-from-class="opacity-100"
        leave-to-class="opacity-0"
      >
        <!-- GitHub -->
        <div
          v-if="activeIntegration === 'github'"
          :key="'github-' + (selectedOrgId ?? 'list')"
        >
          <GitOrgDetail
            v-if="selectedGitOrg"
            :org="selectedGitOrg"
            :workspace-id="workspaceId"
            @back="handleBack"
            @deleted="handleBack(); handleRefresh()"
          />
          <GitHubOverview
            v-else
            :orgs="githubOrgs"
            :workspace-id="workspaceId"
            @select="handleOrgClick"
            @refresh="handleRefresh"
          />
        </div>

        <!-- GitLab -->
        <div
          v-else-if="activeIntegration === 'gitlab'"
          :key="'gitlab-' + (selectedOrgId ?? 'list')"
        >
          <GitOrgDetail
            v-if="selectedGitOrg"
            :org="selectedGitOrg"
            :workspace-id="workspaceId"
            @back="handleBack"
            @deleted="handleBack(); handleRefresh()"
          />
          <GitLabOverview
            v-else
            :orgs="gitlabOrgs"
            :workspace-id="workspaceId"
            @select="handleOrgClick"
            @refresh="handleRefresh"
          />
        </div>

        <!-- Sentry -->
        <div
          v-else-if="activeIntegration === 'sentry'"
          :key="'sentry-' + (selectedOrgId ?? 'list')"
        >
          <SentryOrgDetail
            v-if="selectedSentryOrg"
            :org="selectedSentryOrg"
            :workspace-id="workspaceId"
            @back="handleBack"
            @deleted="handleBack(); handleRefresh()"
          />
          <SentryOverview
            v-else
            :orgs="sentryOrgs"
            :workspace-id="workspaceId"
            @select="handleOrgClick"
            @refresh="handleRefresh"
          />
        </div>
      </Transition>
    </div>
  </div>
</template>

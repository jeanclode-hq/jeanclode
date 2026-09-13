<script setup lang="ts">
const { t } = useI18n()
const { user: authUser, logout } = useAuth()
const workspaceStore = useWorkspaceStore()
const onboarding = useOnboarding()

const navItems = computed(() => [
  [
    { label: t('nav.dashboard'), icon: 'i-lucide-layout-dashboard', to: '/' },
    { label: t('nav.issues'), icon: 'i-lucide-circle-dot', to: '/issues' },
    { label: t('nav.pullRequests'), icon: 'i-lucide-git-pull-request', to: '/pull-requests' },
    { label: t('nav.integrations'), icon: 'i-lucide-plug', to: '/integrations' },
  ],
  [
    { label: t('nav.settings'), icon: 'i-lucide-settings', to: '/settings' },
  ],
])

// Track active route
const route = useRoute()
const navItemsWithActive = computed(() =>
  navItems.value.map((group) =>
    group.map((item) => ({
      ...item,
      active: item.to === '/'
        ? route.path === '/'
        : route.path.startsWith(item.to),
    })),
  ),
)

// Page title from route
const pageTitleMap: Record<string, string> = {
  'index': 'dashboard.title',
  'workspace': 'workspace.title',
  'issues': 'issues.title',
  'pull-requests': 'pullRequests.title',
  'integrations': 'integrations.title',
  'settings': 'settings.title',
}
const pageTitle = computed(() => {
  const name = String(route.name ?? 'index')
  const key = pageTitleMap[name]
  return key ? t(key) : ''
})

// User menu
const userInitial = computed(() => {
  const name = authUser.value?.display_username
  return name ? name.charAt(0).toUpperCase() : 'U'
})

// Show creation modal when user has no workspaces (first login scenario)
const showFirstWorkspaceModal = ref(false)

watch(
  () => workspaceStore.loading,
  (loading) => {
    if (!loading && workspaceStore.workspaces.length === 0) {
      showFirstWorkspaceModal.value = true
    }
  },
  { immediate: true },
)

// Close modal once a workspace is created, then start onboarding
watch(
  () => workspaceStore.workspaces.length,
  (len, prevLen) => {
    if (len > 0) {
      showFirstWorkspaceModal.value = false
    }
    // A workspace was just created — start onboarding
    if (prevLen !== undefined && len > prevLen && workspaceStore.currentWorkspace) {
      onboarding.start()
    }
  },
)

// Check onboarding after workspace loads or on workspace switch
watch(
  () => workspaceStore.currentWorkspace?.id,
  (id) => {
    if (id) {
      onboarding.check()
    }
  },
  { immediate: true },
)

// Global GitHub App claim — works from any page after GitHub redirect
useGitHubClaim()

// SSE stream — connect when workspace is available
let sseConn: ReturnType<typeof useEventStream> | null = null

watch(
  () => workspaceStore.currentWorkspace?.id,
  (wsId) => {
    if (sseConn) {
      sseConn.disconnect()
      sseConn = null
    }
    if (wsId) {
      sseConn = useEventStream(wsId)
      sseConn.connect()
    }
  },
  { immediate: true },
)

onUnmounted(() => {
  if (sseConn) {
    sseConn.disconnect()
    sseConn = null
  }
})
</script>

<template>
  <UDashboardGroup
    storage="cookie"
    storage-key="dashboard-sidebar"
  >
    <!-- Sidebar -->
    <AppSidebar :items="navItemsWithActive" />

    <!-- Main Content -->
    <UDashboardPanel id="main-content">
      <template #header>
        <div class="flex items-center justify-between w-full h-14 px-4 border-b border-neutral-200 dark:border-neutral-700">
          <div class="flex items-center gap-2">
            <UDashboardSidebarToggle
              class="lg:hidden"
              color="neutral"
              variant="ghost"
            />
            <LogoMark class="size-5 shrink-0 text-neutral-900 dark:text-neutral-100 lg:hidden" />
            <span class="text-sm font-semibold text-highlighted lg:hidden">{{ $t('common.appName') }}</span>
            <h1 class="hidden lg:block text-lg font-semibold text-neutral-900 dark:text-neutral-100">
              {{ pageTitle }}
            </h1>
          </div>

          <!-- Trailing: user avatar popover -->
          <UPopover :content="{ align: 'end', side: 'bottom' }">
            <button
              type="button"
              class="cursor-pointer rounded-full p-0.5 hover:ring-2 hover:ring-neutral-200 dark:hover:ring-neutral-700 transition-all"
            >
              <UAvatar
                :src="authUser?.avatar_url ?? undefined"
                :text="userInitial"
                :ui="{ root: !authUser?.avatar_url ? 'bg-gradient-to-br from-neutral-700 to-neutral-900' : '' }"
                size="sm"
              />
            </button>

            <template #content>
              <div class="w-72">
                <!-- User info -->
                <div class="flex items-center gap-3 p-4">
                  <UAvatar
                    :src="authUser?.avatar_url ?? undefined"
                    :text="userInitial"
                    :ui="{ root: !authUser?.avatar_url ? 'bg-gradient-to-br from-neutral-700 to-neutral-900' : '' }"
                    size="lg"
                  />
                  <div class="min-w-0 flex-1">
                    <p class="text-sm font-semibold text-neutral-900 dark:text-neutral-100 truncate">
                      {{ authUser?.display_username ?? $t('user.profile') }}
                    </p>
                    <p class="text-xs text-neutral-500 dark:text-neutral-400 truncate">
                      {{ authUser?.email ?? '' }}
                    </p>
                  </div>
                </div>

                <!-- Workspace -->
                <div
                  v-if="workspaceStore.currentWorkspace"
                  class="px-4 py-3 border-t border-neutral-200 dark:border-neutral-700"
                >
                  <p class="text-xs text-neutral-500 dark:text-neutral-400">
                    {{ $t('workspace.title') }}
                  </p>
                  <p class="text-sm font-medium text-neutral-900 dark:text-neutral-100 truncate">
                    {{ workspaceStore.currentWorkspace.name }}
                  </p>
                </div>

                <!-- Settings + Logout -->
                <div class="border-t border-neutral-200 dark:border-neutral-700 p-2 flex items-center gap-1">
                  <NuxtLink
                    to="/settings"
                    class="flex-1 cursor-pointer flex items-center justify-center gap-2 px-2 py-2 rounded-md text-sm text-neutral-600 dark:text-neutral-400 hover:bg-neutral-100 dark:hover:bg-neutral-800 transition-colors"
                  >
                    <UIcon
                      name="i-lucide-settings"
                      class="size-4"
                    />
                    {{ $t('nav.settings') }}
                  </NuxtLink>
                  <button
                    type="button"
                    class="flex-1 cursor-pointer flex items-center justify-center gap-2 px-2 py-2 rounded-md text-sm text-red-600 dark:text-red-400 hover:bg-red-50 dark:hover:bg-red-950 transition-colors"
                    @click="logout()"
                  >
                    <UIcon
                      name="i-lucide-log-out"
                      class="size-4"
                    />
                    {{ $t('nav.logOut') }}
                  </button>
                </div>
              </div>
            </template>
          </UPopover>
        </div>
      </template>

      <template #body>
        <div class="flex-1 flex flex-col min-h-0">
          <slot />
        </div>
      </template>
    </UDashboardPanel>
  </UDashboardGroup>

  <!-- First workspace creation modal (non-dismissible) -->
  <WorkspaceCreateModal
    v-model:open="showFirstWorkspaceModal"
    :dismissible="false"
  />

  <!-- Onboarding wizard modal -->
  <OnboardingModal />
</template>

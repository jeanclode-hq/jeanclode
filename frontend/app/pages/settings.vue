<script setup lang="ts">
/**
 * User Settings Page
 *
 * Tabbed layout: Account (profile + connected accounts), Workspace (member list + add member).
 */

const { t } = useI18n()
const route = useRoute()
const toast = useToast()

const {
  user: authUser,
  linkProvider,
  disconnectProvider,
  hasProvider,
  connectedProviderCount,
} = useAuth()

const { data: appConfig } = useAppConfigQuery()

useHead({
  title: () => `${t('settings.title')} - Jeanclode`,
})

// Settings tabs
const activeTab = ref(toValue(inject(GUIDE_SETTINGS_TAB, 'account')))
const tabs = computed(() => [
  { id: 'account', label: t('settings.tabs.account'), icon: 'i-lucide-user' },
  { id: 'workspace', label: t('settings.tabs.workspace'), icon: 'i-lucide-building-2' },
])

// Loading states
const disconnectingProvider = ref<string | null>(null)

// Allowed OAuth providers for validation
const ALLOWED_PROVIDERS = ['github', 'gitlab'] as const
type OAuthProvider = typeof ALLOWED_PROVIDERS[number]

// Check for link success/error from query params
onMounted(() => {
  const linked = route.query.linked as string
  const errorParam = route.query.error as string

  if (linked && ALLOWED_PROVIDERS.includes(linked as OAuthProvider)) {
    toast.add({ title: t('settings.accounts.linked', { provider: linked }), color: 'success' })
    navigateTo('/settings', { replace: true })
  } else if (errorParam === 'already_linked') {
    toast.add({ title: t('settings.accounts.alreadyLinked'), color: 'error' })
    navigateTo('/settings', { replace: true })
  } else if (errorParam === 'account_in_use') {
    toast.add({ title: t('settings.accounts.accountInUse'), color: 'error' })
    navigateTo('/settings', { replace: true })
  }
})

// Provider list — a provider with no OAuth app configured on this instance has
// nothing to link against, so only show it when the user is already connected
// (they still need the row to disconnect).
const providerEnabled = computed<Record<OAuthProvider, boolean>>(() => ({
  github: appConfig.value?.providers.github_enabled ?? false,
  gitlab: appConfig.value?.providers.gitlab_enabled ?? false,
}))

const providers = computed(() => [
  {
    id: 'github' as OAuthProvider,
    name: 'GitHub',
    icon: 'i-simple-icons-github',
    connected: hasProvider('github'),
    username: authUser.value?.github_username,
    avatarUrl: authUser.value?.github_external_id
      ? `https://avatars.githubusercontent.com/u/${authUser.value.github_external_id}?size=80`
      : null,
  },
  {
    id: 'gitlab' as OAuthProvider,
    name: 'GitLab',
    icon: 'i-simple-icons-gitlab',
    connected: hasProvider('gitlab'),
    username: authUser.value?.gitlab_username,
    avatarUrl: null,
  },
].filter((p) => p.connected || providerEnabled.value[p.id]))

const canDisconnect = computed(() => connectedProviderCount.value > 1)

function connectProvider(providerId: OAuthProvider) {
  linkProvider(providerId)
}

async function handleDisconnect(providerId: OAuthProvider) {
  if (!canDisconnect.value) {
    toast.add({ title: t('settings.accounts.cannotDisconnectLast'), color: 'error' })
    return
  }

  disconnectingProvider.value = providerId

  try {
    await disconnectProvider(providerId)
    toast.add({ title: t('settings.accounts.disconnected', { provider: providerId }), color: 'success' })
  } catch {
    toast.add({ title: t('settings.accounts.disconnectFailed'), color: 'error' })
  }

  disconnectingProvider.value = null
}

const userInitial = computed(() => {
  const name = authUser.value?.display_username
  return name ? name.charAt(0).toUpperCase() : 'U'
})

const memberSince = computed(() => {
  if (!authUser.value?.created_at) return ''
  return new Date(authUser.value.created_at).toLocaleDateString(undefined, {
    year: 'numeric',
    month: 'long',
    day: 'numeric',
  })
})

// === Workspace tab ===
const workspaceId = useActiveWorkspaceId()

const membersPage = ref(1)
const { data: membersData, isLoading: membersLoading } = useWorkspaceMembersQuery(
  workspaceId,
  membersPage,
)

function formatJoinedDate(date: string) {
  return new Date(date).toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' })
}

function providerIcon(provider: string | null) {
  if (provider === 'github') return 'i-simple-icons-github'
  if (provider === 'gitlab') return 'i-simple-icons-gitlab'
  return 'i-lucide-user'
}
</script>

<template>
  <div class="flex flex-col lg:flex-row h-full px-4 lg:px-0">
    <!-- Settings Tabs -->
    <PageNav
      :items="tabs"
      :model-value="activeTab"
      @update:model-value="activeTab = $event"
    />

    <!-- Settings Content -->
    <div class="flex-1 overflow-y-auto lg:pl-8 space-y-8">
      <template v-if="activeTab === 'account'">
        <!-- Profile -->
        <section>
          <h3 class="text-sm font-semibold text-neutral-900 dark:text-neutral-100 mb-4">
            {{ $t('settings.profile.title') }}
          </h3>
          <UCard>
            <div class="flex items-center gap-5">
              <UAvatar
                :src="authUser?.avatar_url ?? undefined"
                :text="userInitial"
                :ui="{ root: !authUser?.avatar_url ? 'bg-gradient-to-br from-neutral-700 to-neutral-900' : '' }"
                size="3xl"
              />
              <div class="flex-1 min-w-0 space-y-1">
                <p class="text-lg font-semibold text-neutral-900 dark:text-neutral-100 truncate">
                  {{ authUser?.display_username }}
                </p>
                <p
                  v-if="authUser?.email"
                  class="text-sm text-neutral-500 dark:text-neutral-400 truncate"
                >
                  {{ authUser.email }}
                </p>
                <p class="text-xs text-neutral-400 dark:text-neutral-500">
                  {{ $t('settings.profile.memberSince') }} {{ memberSince }}
                </p>
              </div>
            </div>
          </UCard>
        </section>

        <!-- Connected Accounts -->
        <section data-guide="settingsAccounts">
          <h3 class="text-sm font-semibold text-neutral-900 dark:text-neutral-100 mb-2">
            {{ $t('settings.accounts.title') }}
          </h3>
          <p
            v-if="connectedProviderCount === 1"
            class="text-xs text-neutral-500 dark:text-neutral-400 mb-4"
          >
            {{ $t('settings.accounts.lastAccountWarning') }}
          </p>
          <div
            v-else
            class="mb-4"
          />

          <!-- Mobile: compact rows -->
          <UCard class="sm:hidden">
            <div class="divide-y divide-neutral-200 dark:divide-neutral-700">
              <div
                v-for="(provider, index) in providers"
                :key="provider.id"
                class="flex items-center justify-between gap-3"
                :class="index === 0 ? 'pb-3' : index === providers.length - 1 ? 'pt-3' : 'py-3'"
              >
                <div class="flex items-center gap-3 min-w-0">
                  <UAvatar
                    v-if="provider.connected && provider.avatarUrl"
                    :src="provider.avatarUrl"
                    :text="provider.username?.charAt(0).toUpperCase()"
                    size="sm"
                  />
                  <div
                    v-else
                    class="size-8 flex items-center justify-center rounded-full bg-neutral-100 dark:bg-neutral-800 shrink-0"
                  >
                    <UIcon
                      :name="provider.icon"
                      class="size-4"
                    />
                  </div>
                  <div class="min-w-0">
                    <p class="text-sm font-medium text-neutral-900 dark:text-neutral-100">
                      {{ provider.name }}
                    </p>
                    <p class="text-xs text-neutral-500 dark:text-neutral-400 truncate">
                      <template v-if="provider.connected && provider.username">
                        @{{ provider.username }}
                      </template>
                      <template v-else-if="!provider.connected">
                        {{ $t('settings.accounts.notConnected') }}
                      </template>
                      <template v-else>
                        {{ $t('settings.accounts.connected') }}
                      </template>
                    </p>
                  </div>
                </div>
                <UButton
                  v-if="provider.connected"
                  color="neutral"
                  variant="outline"
                  size="sm"
                  icon="i-lucide-unlink"
                  :loading="disconnectingProvider === provider.id"
                  :disabled="!canDisconnect || disconnectingProvider !== null"
                  @click="handleDisconnect(provider.id)"
                >
                  {{ $t('settings.accounts.disconnect') }}
                </UButton>
                <UButton
                  v-else
                  color="primary"
                  size="sm"
                  icon="i-lucide-plug"
                  @click="connectProvider(provider.id)"
                >
                  {{ $t('settings.accounts.connect') }}
                </UButton>
              </div>
            </div>
          </UCard>

          <!-- Desktop: compact rows -->
          <UCard class="hidden sm:block">
            <div class="divide-y divide-neutral-100 dark:divide-neutral-700">
              <div
                v-for="provider in providers"
                :key="provider.id"
                class="flex items-center justify-between gap-4 py-3 first:pt-0 last:pb-0"
              >
                <div class="flex items-center gap-3 min-w-0">
                  <UAvatar
                    v-if="provider.connected && provider.avatarUrl"
                    :src="provider.avatarUrl"
                    :text="provider.username?.charAt(0).toUpperCase()"
                    size="sm"
                  />
                  <div
                    v-else
                    class="size-8 flex items-center justify-center rounded-full bg-neutral-100 dark:bg-neutral-800 shrink-0"
                  >
                    <UIcon
                      :name="provider.icon"
                      class="size-4"
                    />
                  </div>
                  <div class="min-w-0">
                    <p class="text-sm font-medium text-neutral-900 dark:text-neutral-100">
                      {{ provider.name }}
                    </p>
                    <p class="text-xs text-neutral-500 dark:text-neutral-400 truncate">
                      <template v-if="provider.connected && provider.username">
                        @{{ provider.username }}
                      </template>
                      <template v-else-if="!provider.connected">
                        {{ $t('settings.accounts.notConnected') }}
                      </template>
                      <template v-else>
                        {{ $t('settings.accounts.connected') }}
                      </template>
                    </p>
                  </div>
                </div>
                <UButton
                  v-if="provider.connected"
                  color="neutral"
                  variant="outline"
                  size="sm"
                  icon="i-lucide-unlink"
                  :loading="disconnectingProvider === provider.id"
                  :disabled="!canDisconnect || disconnectingProvider !== null"
                  @click="handleDisconnect(provider.id)"
                >
                  {{ $t('settings.accounts.disconnect') }}
                </UButton>
                <UButton
                  v-else
                  color="primary"
                  size="sm"
                  icon="i-lucide-plug"
                  @click="connectProvider(provider.id)"
                >
                  {{ $t('settings.accounts.connect') }}
                </UButton>
              </div>
            </div>
          </UCard>
        </section>
      </template>

      <!-- Workspace tab -->
      <template v-if="activeTab === 'workspace'">
        <section data-guide="settingsMembers">
          <div class="mb-4">
            <h3 class="text-sm font-semibold text-neutral-900 dark:text-neutral-100">
              {{ $t('settings.workspaceTab.title') }}
            </h3>
            <p class="text-xs text-neutral-500 dark:text-neutral-400 mt-0.5">
              {{ $t('settings.workspaceTab.description') }}
            </p>
          </div>

          <!-- Member table -->
          <UCard>
            <div
              v-if="membersLoading"
              class="flex items-center justify-center py-8"
            >
              <UIcon
                name="i-lucide-loader-2"
                class="size-5 animate-spin text-neutral-400"
              />
            </div>
            <div
              v-else-if="!membersData?.members.length"
              class="py-8 text-center"
            >
              <p class="text-sm text-neutral-500 dark:text-neutral-400">
                {{ $t('common.noResults') }}
              </p>
            </div>
            <div
              v-else
              class="overflow-x-auto"
            >
              <table class="w-full text-sm">
                <thead>
                  <tr class="border-b border-neutral-200 dark:border-neutral-700">
                    <th class="text-left py-2 pr-4 font-medium text-neutral-500 dark:text-neutral-400 text-xs uppercase tracking-wider">
                      {{ $t('settings.workspaceTab.columns.member') }}
                    </th>
                    <th class="text-left py-2 pr-4 font-medium text-neutral-500 dark:text-neutral-400 text-xs uppercase tracking-wider">
                      {{ $t('settings.workspaceTab.columns.providers') }}
                    </th>
                    <th class="text-left py-2 font-medium text-neutral-500 dark:text-neutral-400 text-xs uppercase tracking-wider">
                      {{ $t('settings.workspaceTab.columns.joined') }}
                    </th>
                  </tr>
                </thead>
                <tbody class="divide-y divide-neutral-100 dark:divide-neutral-700">
                  <tr
                    v-for="member in membersData.members"
                    :key="member.user_id"
                  >
                    <td class="py-3 pr-4">
                      <div class="flex items-center gap-2.5">
                        <UAvatar
                          :src="member.avatar_url ?? undefined"
                          :text="member.username.charAt(0).toUpperCase()"
                          size="sm"
                        />
                        <span class="font-medium text-neutral-900 dark:text-neutral-100">
                          {{ member.username }}
                        </span>
                      </div>
                    </td>
                    <td class="py-3 pr-4">
                      <div
                        v-if="member.providers.length"
                        class="flex items-center gap-2"
                      >
                        <UIcon
                          v-for="p in member.providers"
                          :key="p"
                          :name="providerIcon(p)"
                          :title="p"
                          class="size-4 text-neutral-500"
                        />
                      </div>
                      <span
                        v-else
                        class="text-neutral-400"
                      >—</span>
                    </td>
                    <td class="py-3 text-neutral-500 dark:text-neutral-400">
                      {{ formatJoinedDate(member.joined_at) }}
                    </td>
                  </tr>
                </tbody>
              </table>

              <!-- Pagination -->
              <div
                v-if="membersData.total > 20"
                class="flex items-center justify-between pt-4 mt-2 border-t border-neutral-100 dark:border-neutral-700"
              >
                <p class="text-xs text-neutral-500">
                  {{ membersData.total }} members
                </p>
                <div class="flex gap-2">
                  <UButton
                    icon="i-lucide-chevron-left"
                    size="xs"
                    color="neutral"
                    variant="outline"
                    :disabled="membersPage === 1"
                    @click="membersPage--"
                  />
                  <UButton
                    icon="i-lucide-chevron-right"
                    size="xs"
                    color="neutral"
                    variant="outline"
                    :disabled="!membersData.has_more"
                    @click="membersPage++"
                  />
                </div>
              </div>
            </div>
          </UCard>
        </section>
      </template>
    </div>
  </div>
</template>

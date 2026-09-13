<script setup lang="ts">
import type { CredentialStatus, MarketplaceEntry, MarketplacePluginEntry } from '@jeanclode/api-types'

const props = defineProps<{
  orgId: string
}>()

const { t } = useI18n()
const toast = useToast()

const authTypeLabels: Record<string, string> = {
  api_key: t('connectors.authType.apiKey'),
  jwt: t('connectors.authType.jwt'),
  basic_auth: t('connectors.authType.basicAuth'),
  oauth2: t('connectors.authType.oauth2'),
}

function authBadgeTooltip(credential: CredentialStatus): string {
  const label = authTypeLabels[credential.auth_type] ?? credential.auth_type
  const host = credential.settings && typeof credential.settings.host === 'string' ? credential.settings.host : null
  return host ? `${label} · ${host}` : label
}

// Queries + mutations
const orgIdRef = computed(() => props.orgId)
const { data: overview, isLoading } = usePluginsOverviewQuery(orgIdRef)
const connectMutation = useConnectMarketplaceMutation()
const disconnectMutation = useDisconnectMarketplaceMutation()
const installMutation = useInstallPluginMutation()
const uninstallMutation = useUninstallPluginMutation()

// Derived data
const marketplaces = computed(() => overview.value?.marketplaces ?? [])
const installed = computed(() => overview.value?.installed ?? [])

// Pre-computed lookup: marketplace_id → set of installed plugin names
const installedByMarket = computed(() => {
  const map = new Map<string, Set<string>>()
  for (const i of installed.value) {
    if (!map.has(i.marketplace_id)) map.set(i.marketplace_id, new Set())
    map.get(i.marketplace_id)!.add(i.plugin_name)
  }
  return map
})

// Pre-computed lookup: (marketplace_id, plugin_name) → install id
const installIdMap = computed(() => {
  const map = new Map<string, string>()
  for (const i of installed.value) {
    map.set(`${i.marketplace_id}:${i.plugin_name}`, i.id)
  }
  return map
})

// Each install carries its own stored auth, so the manifest list reads it off
// the overview it already has instead of one credential request per plugin.
const credentialByInstall = computed(() => {
  const map = new Map<string, CredentialStatus>()
  for (const i of installed.value) {
    if (i.credential) map.set(i.id, i.credential)
  }
  return map
})

function credentialFor(marketId: string, pluginName: string): CredentialStatus | undefined {
  const installId = installIdMap.value.get(`${marketId}:${pluginName}`)
  return installId ? credentialByInstall.value.get(installId) : undefined
}

function installedCount(market: MarketplaceEntry): number {
  return installedByMarket.value.get(market.id)?.size ?? 0
}

function installableCount(market: MarketplaceEntry): number {
  const set = installedByMarket.value.get(market.id)
  return (market.plugins ?? []).filter((p) => !set?.has(p.name)).length
}

// Accordion — one open at a time
const expandedId = ref<string | null>(null)
function toggleMarketplace(id: string) {
  expandedId.value = expandedId.value === id ? null : id
}

// Per-installed-plugin auth form — one open at a time, keyed by install id.
const authFormOpenFor = ref<string | null>(null)
function toggleAuthForm(installId: string) {
  authFormOpenFor.value = authFormOpenFor.value === installId ? null : installId
}

// Add marketplace — button morphs into input
const addExpanded = ref(false)
const addUrl = ref('')
const addInputRef = ref<{ inputRef?: HTMLInputElement } | null>(null)

function expandAdd() {
  addExpanded.value = true
  nextTick(() => addInputRef.value?.inputRef?.focus())
}

function collapseAdd() {
  addExpanded.value = false
  addUrl.value = ''
}

// Per-item loading state
const installingNames = ref(new Set<string>())
const installingAllMarket = ref<string | null>(null)

// Actions
async function submitConnect() {
  if (!addUrl.value.trim()) return
  try {
    await connectMutation.mutateAsync({ orgId: props.orgId, gitUrl: addUrl.value.trim() })
    toast.add({ title: t('plugins.toast.marketplaceConnected'), color: 'success' })
    collapseAdd()
  } catch (e) {
    toast.add({ title: t('plugins.toast.connectFailed'), description: extractApiError(e, t('plugins.toast.connectFailed')), color: 'error' })
  }
}

async function disconnect(market: MarketplaceEntry) {
  try {
    await disconnectMutation.mutateAsync({ orgId: props.orgId, marketplaceId: market.id })
    toast.add({ title: t('plugins.toast.marketplaceDisconnected'), color: 'success' })
  } catch (e) {
    toast.add({ title: t('plugins.toast.disconnectFailed'), description: extractApiError(e, t('plugins.toast.disconnectFailed')), color: 'error' })
  }
}

async function installOne(market: MarketplaceEntry, plugin: MarketplacePluginEntry) {
  installingNames.value.add(plugin.name)
  try {
    await installMutation.mutateAsync({ orgId: props.orgId, marketplaceId: market.id, pluginNames: [plugin.name] })
    toast.add({ title: t('plugins.toast.pluginInstalled'), color: 'success' })
  } catch (e) {
    toast.add({ title: t('plugins.toast.installFailed'), description: extractApiError(e, t('plugins.toast.installFailed')), color: 'error' })
  } finally {
    installingNames.value.delete(plugin.name)
  }
}

async function installAll(market: MarketplaceEntry) {
  const names = (market.plugins ?? []).filter((p) => !p.installed).map((p) => p.name)
  if (!names.length) return
  installingAllMarket.value = market.id
  try {
    await installMutation.mutateAsync({ orgId: props.orgId, marketplaceId: market.id, pluginNames: names })
    toast.add({ title: t('plugins.toast.allInstalled'), color: 'success' })
  } catch (e) {
    toast.add({ title: t('plugins.toast.installFailed'), description: extractApiError(e, t('plugins.toast.installFailed')), color: 'error' })
  } finally {
    installingAllMarket.value = null
  }
}

async function uninstallOne(market: MarketplaceEntry, pluginName: string) {
  const installId = installIdMap.value.get(`${market.id}:${pluginName}`)
  if (!installId) return
  try {
    await uninstallMutation.mutateAsync({ orgId: props.orgId, installId })
    toast.add({ title: t('plugins.toast.pluginUninstalled'), color: 'success' })
  } catch (e) {
    toast.add({ title: t('plugins.toast.uninstallFailed'), description: extractApiError(e, t('plugins.toast.uninstallFailed')), color: 'error' })
  }
}

async function uninstallAllFromMarketplace(market: MarketplaceEntry) {
  const ids = installed.value
    .filter((i) => i.marketplace_id === market.id)
    .map((i) => i.id)
  for (const id of ids) {
    try {
      await uninstallMutation.mutateAsync({ orgId: props.orgId, installId: id })
    } catch (e) {
      toast.add({ title: t('plugins.toast.uninstallFailed'), description: extractApiError(e, t('plugins.toast.uninstallFailed')), color: 'error' })
      return
    }
  }
  toast.add({ title: t('plugins.toast.allUninstalled'), color: 'success' })
}
</script>

<template>
  <section>
    <div class="flex items-center justify-between mb-3">
      <h4 class="text-sm font-semibold text-neutral-900 dark:text-neutral-100">
        {{ t('plugins.title') }}
      </h4>

      <!-- Button ↔ Input morph -->
      <div class="relative h-7 flex items-center">
        <form
          v-if="addExpanded"
          class="add-input flex items-center gap-1.5"
          @submit.prevent="submitConnect"
        >
          <UInput
            ref="addInputRef"
            v-model="addUrl"
            :placeholder="t('plugins.urlPlaceholder')"
            size="xs"
            class="w-64"
            @keydown.escape="collapseAdd"
          />
          <UButton
            type="submit"
            icon="i-lucide-arrow-right"
            size="xs"
            variant="soft"
            :loading="connectMutation.isLoading.value"
            :disabled="!addUrl.trim()"
          />
          <UButton
            icon="i-lucide-x"
            size="xs"
            color="neutral"
            variant="ghost"
            @click="collapseAdd"
          />
        </form>
        <UButton
          v-else
          icon="i-lucide-plus"
          :label="t('plugins.connectMarketplace')"
          size="xs"
          color="neutral"
          variant="outline"
          class="add-button"
          @click="expandAdd"
        />
      </div>
    </div>

    <p class="text-xs text-neutral-500 dark:text-neutral-400 mb-3">
      {{ t('plugins.subtitle') }}
    </p>

    <!-- Loading -->
    <div
      v-if="isLoading && !overview"
      class="rounded-xl border border-neutral-200 dark:border-neutral-700 bg-white dark:bg-neutral-800 shadow-xs dark:shadow-none p-8 flex items-center justify-center"
    >
      <UIcon
        name="i-lucide-loader-2"
        class="size-5 text-neutral-400 animate-spin"
      />
    </div>

    <template v-else>
      <!-- Empty state -->
      <div
        v-if="marketplaces.length === 0 && installed.length === 0"
        class="rounded-xl border border-dashed border-neutral-200 dark:border-neutral-700 p-6 text-center text-xs text-neutral-500"
      >
        {{ t('plugins.empty') }}
      </div>

      <!-- Marketplace accordions -->
      <div
        v-for="market in marketplaces"
        :key="market.id"
        class="rounded-xl border border-neutral-200 dark:border-neutral-700 bg-white dark:bg-neutral-800 shadow-xs dark:shadow-none mb-3 overflow-hidden"
      >
        <button
          type="button"
          class="w-full cursor-pointer flex items-center justify-between gap-3 px-4 py-3 hover:bg-neutral-50 dark:hover:bg-neutral-700/50 transition-colors"
          @click="toggleMarketplace(market.id)"
        >
          <div class="min-w-0 text-left">
            <div class="flex items-center gap-2">
              <UIcon
                name="i-lucide-package"
                class="size-3.5 text-neutral-400 shrink-0"
              />
              <h5 class="text-sm font-medium truncate">
                {{ market.name }}
              </h5>
              <span
                v-if="market.plugins?.length"
                class="text-[10px] text-neutral-500"
              >{{ market.plugins.length }} {{ market.plugins.length === 1 ? 'plugin' : 'plugins' }}</span>
              <span
                v-if="installedCount(market)"
                class="text-[10px] px-1.5 py-0.5 rounded-full bg-neutral-200 dark:bg-neutral-600 text-neutral-700 dark:text-neutral-200 font-medium"
              >{{ installedCount(market) }} installed</span>
              <span
                v-if="market.last_sync_status === 'error'"
                class="text-[10px] uppercase tracking-wide px-1.5 py-0.5 rounded bg-red-100 dark:bg-red-950 text-red-700 dark:text-red-400"
              >{{ t('plugins.unreachable') }}</span>
            </div>
            <p class="text-xs text-neutral-500 dark:text-neutral-400 truncate font-mono">
              {{ market.git_url }}
            </p>
          </div>
          <div class="flex items-center gap-1 shrink-0">
            <UButton
              v-if="installedCount(market)"
              :label="t('plugins.uninstallAll')"
              size="xs"
              color="error"
              variant="soft"
              :loading="uninstallMutation.isLoading.value"
              @click.stop="uninstallAllFromMarketplace(market)"
            />
            <UButton
              v-if="installableCount(market) > 1"
              :label="t('plugins.installAll')"
              size="xs"
              variant="soft"
              :loading="installingAllMarket === market.id"
              @click.stop="installAll(market)"
            />
            <UButton
              icon="i-lucide-x"
              variant="ghost"
              color="neutral"
              size="xs"
              :aria-label="t('plugins.disconnect')"
              @click.stop="disconnect(market)"
            />
            <UIcon
              name="i-lucide-chevron-down"
              class="size-4 text-neutral-400 transition-transform duration-200"
              :class="{ 'rotate-180': expandedId === market.id }"
            />
          </div>
        </button>

        <div v-if="expandedId === market.id">
          <div
            v-if="market.plugins === null"
            class="px-4 py-3 text-xs text-red-600 dark:text-red-400 border-t border-neutral-200 dark:border-neutral-700"
          >
            {{ market.last_sync_error ?? t('plugins.fetchFailed') }}
          </div>
          <div
            v-else-if="market.plugins.length === 0"
            class="px-4 py-3 text-xs text-neutral-500 italic border-t border-neutral-200 dark:border-neutral-700"
          >
            {{ t('plugins.emptyManifest') }}
          </div>
          <ul
            v-else
            class="divide-y divide-neutral-200 dark:divide-neutral-700 border-t border-neutral-200 dark:border-neutral-700"
          >
            <li
              v-for="plugin in market.plugins"
              :key="plugin.name"
              class="px-4 py-2.5"
            >
              <div class="flex items-center justify-between gap-3">
                <div class="min-w-0">
                  <div class="text-sm truncate text-neutral-700 dark:text-neutral-300">
                    {{ plugin.name }}
                  </div>
                  <p
                    v-if="plugin.description"
                    class="text-xs text-neutral-500 dark:text-neutral-400 truncate"
                  >
                    {{ plugin.description }}
                  </p>
                </div>
                <div class="flex items-center gap-1.5 shrink-0">
                  <template v-if="plugin.installed">
                    <UTooltip
                      v-if="credentialFor(market.id, plugin.name)"
                      :text="authBadgeTooltip(credentialFor(market.id, plugin.name)!)"
                    >
                      <UBadge
                        :label="t('connectors.authConfigured')"
                        color="success"
                        variant="subtle"
                        size="xs"
                      />
                    </UTooltip>
                    <UButton
                      :label="credentialFor(market.id, plugin.name) ? t('connectors.editAuth') : t('plugins.addAuth')"
                      size="xs"
                      variant="soft"
                      @click="toggleAuthForm(installIdMap.get(`${market.id}:${plugin.name}`)!)"
                    />
                    <UButton
                      icon="i-lucide-trash-2"
                      variant="ghost"
                      color="error"
                      size="xs"
                      @click="uninstallOne(market, plugin.name)"
                    />
                  </template>
                  <UButton
                    v-else
                    :label="t('plugins.install')"
                    size="xs"
                    :loading="installingNames.has(plugin.name)"
                    @click="installOne(market, plugin)"
                  />
                </div>
              </div>
              <div
                v-if="plugin.installed && authFormOpenFor === installIdMap.get(`${market.id}:${plugin.name}`)"
                class="mt-2"
              >
                <AddAuthForm
                  :org-id="orgId"
                  subject-type="plugin_installation"
                  :subject-id="installIdMap.get(`${market.id}:${plugin.name}`)!"
                  @close="authFormOpenFor = null"
                />
              </div>
            </li>
          </ul>
        </div>
      </div>
    </template>
  </section>
</template>

<style scoped>
.add-button,
.add-input {
  animation: morph-in 200ms ease-out both;
}

@keyframes morph-in {
  from { opacity: 0; transform: scale(0.95); }
  to { opacity: 1; transform: scale(1); }
}
</style>

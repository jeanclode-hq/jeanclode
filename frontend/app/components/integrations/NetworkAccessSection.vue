<script setup lang="ts">
import type { NetworkHostResponse, NetworkHostSource } from '@jeanclode/api-types'

const props = defineProps<{
  orgId: string
}>()

const { t } = useI18n()
const toast = useToast()

const orgIdRef = computed(() => props.orgId)
const { data: hosts, isLoading } = useOrgNetworkHostsQuery(orgIdRef)
const { data: settings } = useOrgSettingsQuery(orgIdRef)
const settingsMutation = useUpdateOrgSettingsMutation()

const extraHosts = computed<string[]>(
  () => (settings.value as { network?: { extra_hosts?: string[] } } | undefined)?.network?.extra_hosts ?? [],
)

const SOURCES: NetworkHostSource[] = ['org', 'skill', 'mcp_server', 'platform', 'builtin']

const SOURCE_ICONS: Record<NetworkHostSource, string> = {
  org: 'i-lucide-globe',
  skill: 'i-lucide-sparkles',
  mcp_server: 'i-lucide-plug',
  platform: 'i-lucide-server',
  builtin: 'i-lucide-box',
}

const groups = computed(() =>
  SOURCES.map((source) => ({
    source,
    rows: (hosts.value ?? []).filter((h) => h.source === source),
  })).filter((g) => g.rows.length),
)

function workflowsLabel(row: NetworkHostResponse): string | null {
  if (!row.workflows?.length) return null
  const names = row.workflows.map((w) => t(`network.workflows.${w}`, w))
  return t('network.onlyFor', { workflows: names.join(', ') })
}

const addExpanded = ref(false)
const addHost = ref('')

function collapseAdd() {
  addExpanded.value = false
  addHost.value = ''
}

async function saveHosts(next: string[], success: string) {
  try {
    await settingsMutation.mutateAsync({ orgId: props.orgId, settings: { network: { extra_hosts: next } } })
    toast.add({ title: success, color: 'success' })
    return true
  } catch (e) {
    toast.add({ title: t('network.saveFailed'), description: extractApiError(e, t('network.saveFailed')), color: 'error' })
    return false
  }
}

async function submitAdd() {
  const host = addHost.value.trim()
  if (!host) return
  if (await saveHosts([...extraHosts.value, host], t('network.hostAdded'))) collapseAdd()
}

function remove(host: string) {
  saveHosts(extraHosts.value.filter((h) => h !== host), t('network.hostRemoved'))
}
</script>

<template>
  <section>
    <div class="flex items-center justify-between mb-3">
      <h4 class="text-sm font-semibold text-neutral-900 dark:text-neutral-100">
        {{ t('network.title') }}
      </h4>

      <div class="relative h-7 flex items-center">
        <form
          v-if="addExpanded"
          class="flex items-center gap-1.5"
          @submit.prevent="submitAdd"
        >
          <UInput
            v-model="addHost"
            :placeholder="t('network.hostPlaceholder')"
            size="xs"
            class="w-60"
            autofocus
          />
          <UButton
            type="submit"
            icon="i-lucide-arrow-right"
            size="xs"
            variant="soft"
            :loading="settingsMutation.isLoading.value"
            :disabled="!addHost.trim()"
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
          :label="t('network.addHost')"
          size="xs"
          color="neutral"
          variant="outline"
          @click="addExpanded = true"
        />
      </div>
    </div>

    <p class="text-xs text-neutral-500 dark:text-neutral-400 mb-3">
      {{ t('network.subtitle') }}
    </p>

    <div
      v-if="isLoading && !hosts"
      class="rounded-xl border border-neutral-200 dark:border-neutral-700 bg-white dark:bg-neutral-800 shadow-xs dark:shadow-none p-8 flex items-center justify-center"
    >
      <UIcon
        name="i-lucide-loader-2"
        class="size-5 text-neutral-400 animate-spin"
      />
    </div>

    <div
      v-else-if="!groups.length"
      class="rounded-xl border border-dashed border-neutral-200 dark:border-neutral-700 p-6 text-center text-xs text-neutral-500"
    >
      {{ t('network.empty') }}
    </div>

    <div
      v-else
      class="rounded-xl border border-neutral-200 dark:border-neutral-700 bg-white dark:bg-neutral-800 shadow-xs dark:shadow-none divide-y divide-neutral-200 dark:divide-neutral-700 overflow-hidden"
    >
      <div
        v-for="group in groups"
        :key="group.source"
      >
        <div class="flex items-center gap-2 px-4 pt-3 pb-1">
          <UIcon
            :name="SOURCE_ICONS[group.source]"
            class="size-3.5 text-neutral-400 shrink-0"
          />
          <span class="text-[11px] font-medium uppercase tracking-wide text-neutral-500 dark:text-neutral-400">
            {{ t(`network.sources.${group.source}`) }}
          </span>
        </div>
        <ul class="pb-2">
          <li
            v-for="row in group.rows"
            :key="`${row.host}-${row.label}`"
            class="flex items-center justify-between gap-3 px-4 py-1.5"
          >
            <div class="min-w-0 flex items-baseline gap-2 flex-wrap">
              <span class="text-sm font-mono text-neutral-700 dark:text-neutral-300 truncate">{{ row.host }}</span>
              <span
                v-if="group.source !== 'org'"
                class="text-xs text-neutral-500 dark:text-neutral-400"
              >{{ row.label }}</span>
              <span
                v-if="workflowsLabel(row)"
                class="text-[11px] text-neutral-400 dark:text-neutral-500"
              >{{ workflowsLabel(row) }}</span>
            </div>
            <div class="flex items-center gap-1 shrink-0">
              <UTooltip
                v-if="row.authenticated"
                :text="t('network.authenticated')"
              >
                <UIcon
                  name="i-lucide-key-round"
                  class="size-3.5 text-neutral-400"
                />
              </UTooltip>
              <UButton
                v-if="group.source === 'org'"
                icon="i-lucide-trash-2"
                variant="ghost"
                color="error"
                size="xs"
                :loading="settingsMutation.isLoading.value"
                @click="remove(row.host)"
              />
            </div>
          </li>
        </ul>
      </div>
    </div>
  </section>
</template>

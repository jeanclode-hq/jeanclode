<script setup lang="ts">
import { listSourceProjects } from '@jeanclode/api-types'
import type { BackfillScope, BatchSize, SentryProjectResponse, RepoResponse } from '@jeanclode/api-types'
import type { Organization } from '~/types/api'

const props = defineProps<{
  org: Organization
  workspaceId: string
}>()

const emit = defineEmits<{
  back: []
  deleted: []
}>()

const { t } = useI18n()
const client = useApi()
const toast = useToast()

// Delete
const deleteMutation = useDeleteOrgMutation()
const isDeleting = deleteMutation.isLoading

async function handleDelete() {
  try {
    await deleteMutation.mutateAsync(props.org.id)
    emit('deleted')
  } catch {
    toast.add({ title: 'Delete failed', description: 'Failed to disconnect the Sentry organization', color: 'error' })
  }
}

// Fetch projects
const { data: projects, status: projectsStatus, refresh: refreshProjects } = useQuery({
  key: () => ['source-projects', props.org.id],
  query: async () => {
    const { data } = await listSourceProjects({ client, query: { org_id: props.org.id } })
    // Clear resolving state when SSE triggers a refetch with fresh data
    if (isResolving.value) isResolving.value = false
    return data ?? []
  },
})

// Fetch git orgs for the repo picker
const { data: gitOrgs } = useOrgsQuery(() => props.workspaceId, ['github', 'gitlab'])

// Fetch all repos across orgs for name lookup on mapped projects
const { data: allRepos } = useQuery({
  key: () => ['repos', 'workspace', props.workspaceId],
  query: async () => {
    const orgs = gitOrgs.value ?? []
    const results = await Promise.all(orgs.map((org) => fetchAllOrgRepos(client, org.id)))
    return results.flat()
  },
  enabled: () => (gitOrgs.value ?? []).length > 0,
})

// O(1) lookup map for repo names
const repoNameMap = computed(() => {
  const map = new Map<string, string>()
  for (const repo of (allRepos.value ?? []) as RepoResponse[]) {
    map.set(repo.id, repo.name)
  }
  return map
})

// Mapping mutations
const projectMutation = useUpdateSourceProjectMutation()
const resolveMutation = useResolveProjectMappingsMutation()
const isResolving = ref(false)

// All git orgs for the repo picker
const availableGitOrgs = computed(() => gitOrgs.value ?? [])

type BadgeColor = 'success' | 'warning' | 'info' | 'neutral'

function sourceBadgeColor(source: string | null): BadgeColor {
  switch (source) {
    case 'code_mapping': return 'success'
    case 'fuzzy': return 'warning'
    case 'manual': return 'info'
    default: return 'neutral'
  }
}

function sourceBadgeLabel(source: string | null): string {
  if (!source) return t('onboarding.mapping.unmapped')
  const key = `onboarding.mapping.sources.${source}` as const
  return t(key)
}

function repoName(repoId: string | null): string {
  if (!repoId) return ''
  const name = repoNameMap.value.get(repoId)
  return name ? shortRepoName(name) : ''
}

async function handleRepoChange(project: SentryProjectResponse, repoId: string) {
  await projectMutation.mutateAsync({ projectId: project.id, repoId: repoId || null })
  refreshProjects()
}

async function handleUnmap(project: SentryProjectResponse) {
  await projectMutation.mutateAsync({ projectId: project.id, repoId: null })
  refreshProjects()
}

// Filtering & pagination
type FilterTab = 'all' | 'unmapped' | 'mapped'
const activeFilter = ref<FilterTab>('all')
const searchQuery = ref('')
const currentPage = ref(1)
const PAGE_SIZE = 25

const unmappedCount = computed(() => projects.value?.filter((p) => !p.mapped_repo_id).length ?? 0)

const filteredProjects = computed(() => {
  let list = projects.value ?? []
  if (activeFilter.value === 'unmapped') list = list.filter((p) => !p.mapped_repo_id)
  else if (activeFilter.value === 'mapped') list = list.filter((p) => !!p.mapped_repo_id)
  if (searchQuery.value.trim()) {
    const q = searchQuery.value.toLowerCase()
    list = list.filter((p) => p.name.toLowerCase().includes(q))
  }
  return list
})

const totalPages = computed(() => Math.ceil(filteredProjects.value.length / PAGE_SIZE))
const paginatedProjects = computed(() => {
  const start = (currentPage.value - 1) * PAGE_SIZE
  return filteredProjects.value.slice(start, start + PAGE_SIZE)
})

// Reset to page 1 when filter/search changes
watch([activeFilter, searchQuery], () => {
  currentPage.value = 1
})

const mappingProgress = computed(() => {
  if (!totalCount.value) return 0
  return Math.round((mappedCount.value / totalCount.value) * 100)
})

async function handleReResolve() {
  isResolving.value = true
  const minDelay = new Promise((r) => setTimeout(r, 1500))
  try {
    await resolveMutation.mutateAsync({ orgId: props.org.id })
    // Wait for both SSE refresh and min animation time
    await minDelay
  } catch {
    await minDelay
  } finally {
    isResolving.value = false
  }
}

const mappedCount = computed(() =>
  projects.value?.filter((p) => p.mapped_repo_id).length ?? 0,
)
const totalCount = computed(() => projects.value?.length ?? 0)
const isLoadingProjects = computed(() => projectsStatus.value === 'loading')

// Settings
const { data: settings } = useOrgSettingsQuery(() => props.org.id)
const settingsMutation = useUpdateOrgSettingsMutation()

const triageOptions = [
  { label: 'Manual only', value: 'manual' },
  { label: 'Automatic', value: 'automatic' },
]

const triageHints: Record<string, string> = {
  manual: 'Issues are only dispatched for fixing manually from the dashboard.',
  automatic: 'Issues are automatically batched and dispatched to fix agents on a timed window.',
}

// Cast: this component only ever renders sentry orgs, but the settings
// query's type is the git/sentry union — same reason GitOrgDetail casts
// manageProjectWebhooks.
const triageValue = computed(
  () => (settings.value as { triggers?: { triage?: string } } | undefined)?.triggers?.triage ?? 'manual',
)

async function updateTriage(value: string) {
  if (!settings.value) return
  try {
    await settingsMutation.mutateAsync({
      orgId: props.org.id,
      settings: { triggers: { triage: value } },
    })
  } catch {
    toast.add({ title: 'Failed to save', description: 'Could not update settings', color: 'error' })
  }
}

// Batch window — how long the dispatcher waits between batches, so related
// errors have time to accumulate into one PR instead of one per error.
const batchWindowOptions = [
  { label: '5 minutes', value: '5m' },
  { label: '1 hour', value: '1h' },
  { label: '12 hours', value: '12h' },
  { label: '1 day', value: '1d' },
  { label: '3 days', value: '3d' },
  { label: '1 week', value: '1w' },
]

async function updateBatchWindow(value: string) {
  if (!settings.value) return
  try {
    await settingsMutation.mutateAsync({
      orgId: props.org.id,
      settings: { batch_window: value },
    })
  } catch {
    toast.add({ title: 'Failed to save', description: 'Could not update settings', color: 'error' })
  }
}

// Batch size — how many issues one dispatch batch may carry for this org.
const batchSizeOptions = [
  { label: '1 issue', value: '1' },
  { label: '3 issues', value: '3' },
  { label: '5 issues', value: '5' },
  { label: '10 issues', value: '10' },
]

const batchSizeValue = computed(
  () => (settings.value as { batch_size?: string } | undefined)?.batch_size ?? '5',
)

async function updateBatchSize(value: string) {
  if (!settings.value) return
  try {
    await settingsMutation.mutateAsync({
      orgId: props.org.id,
      settings: { batch_size: value as BatchSize },
    })
  } catch {
    toast.add({ title: 'Failed to save', description: 'Could not update settings', color: 'error' })
  }
}

// Merge gate — don't start the next batch until every PR the previous one
// opened has been merged or closed. Off by default.
const gateOnOpenFixPrs = computed(
  () => (settings.value as { gate_on_open_fix_prs?: boolean } | undefined)?.gate_on_open_fix_prs ?? false,
)

async function updateGateOnOpenFixPrs(value: boolean) {
  if (!settings.value) return
  try {
    await settingsMutation.mutateAsync({
      orgId: props.org.id,
      settings: { gate_on_open_fix_prs: value },
    })
  } catch {
    toast.add({ title: 'Failed to save', description: 'Could not update settings', color: 'error' })
  }
}

// Backfill — how much existing Sentry history to import. Editable after
// onboarding so a skipped import can be run later.
const backfillMutation = useBackfillMutation()
const isImporting = backfillMutation.isLoading

const scopeOptions = computed(() => backfillOptions(t))
const currentScope = computed<BackfillScope>(
  () => (settings.value as { backfill?: BackfillScope } | undefined)?.backfill ?? DEFAULT_BACKFILL_SCOPE,
)
const scopeHint = computed(() => backfillHint(t, currentScope.value))

async function updateBackfillScope(value: string) {
  try {
    await settingsMutation.mutateAsync({
      orgId: props.org.id,
      settings: { backfill: value as BackfillScope },
    })
  } catch {
    toast.add({ title: 'Failed to save', description: 'Could not update settings', color: 'error' })
  }
}

async function handleImportNow() {
  try {
    await backfillMutation.mutateAsync({ orgId: props.org.id })
    toast.add({ title: t('onboarding.sentry.backfillQueued'), color: 'success' })
  } catch (e: unknown) {
    toast.add({
      title: t('onboarding.sentry.backfillQueuedFailed'),
      description: extractApiError(e, 'Failed to queue the import'),
      color: 'error',
    })
  }
}
</script>

<template>
  <div class="space-y-6">
    <!-- Back breadcrumb -->
    <button
      type="button"
      class="cursor-pointer flex items-center gap-1.5 text-xs text-neutral-500 dark:text-neutral-400 hover:text-neutral-700 dark:hover:text-neutral-300 transition-colors"
      @click="emit('back')"
    >
      <UIcon
        name="i-lucide-arrow-left"
        class="size-3.5"
      />
      Back to Sentry
    </button>

    <!-- Org header -->
    <div class="rounded-xl border border-neutral-200 dark:border-neutral-700 p-5">
      <div class="flex items-start gap-4">
        <div class="size-12 rounded-xl bg-neutral-100 dark:bg-neutral-800 flex items-center justify-center shrink-0">
          <ProviderIcon
            light-src="/icons/sentry-light.svg"
            dark-src="/icons/sentry-dark.svg"
            alt="Sentry"
            class="size-6"
          />
        </div>
        <div class="flex-1 min-w-0">
          <h3 class="text-lg font-semibold text-neutral-900 dark:text-neutral-100">
            {{ org.name }}
          </h3>
          <p class="text-sm text-neutral-500 dark:text-neutral-400 mt-0.5">
            {{ org.external_org_id }} &middot; {{ org.repo_count }} {{ org.repo_count === 1 ? 'project' : 'projects' }}
            <template v-if="org.base_url !== 'https://sentry.io'">
              &middot; {{ org.base_url }}
            </template>
          </p>
        </div>
      </div>
    </div>

    <!-- Project mappings -->
    <section>
      <div class="flex items-center justify-between mb-3">
        <div>
          <h4 class="text-sm font-semibold text-neutral-900 dark:text-neutral-100">
            Project Mappings
          </h4>
        </div>
        <UButton
          v-if="projects && projects.length > 0"
          :label="isResolving ? 'Resolving...' : 'Auto-resolve'"
          :loading="isResolving"
          variant="outline"
          color="neutral"
          icon="i-lucide-sparkles"
          size="xs"
          @click="handleReResolve"
        />
      </div>

      <div
        v-if="isLoadingProjects"
        class="rounded-xl border border-neutral-200 dark:border-neutral-700 p-8 flex items-center justify-center"
      >
        <UIcon
          name="i-lucide-loader-2"
          class="size-5 text-neutral-400 animate-spin"
        />
      </div>

      <div
        v-else-if="!projects || projects.length === 0"
        class="rounded-xl border border-neutral-200 dark:border-neutral-700 border-dashed p-8 text-center"
      >
        <UIcon
          name="i-lucide-loader-2"
          class="size-5 text-neutral-400 animate-spin mx-auto mb-2"
        />
        <p class="text-xs text-neutral-500 dark:text-neutral-400">
          Syncing projects from Sentry...
        </p>
      </div>

      <div
        v-else
        class="space-y-3"
      >
        <!-- Progress bar -->
        <div class="space-y-1.5">
          <div class="flex items-center justify-between text-xs">
            <span class="text-neutral-500 dark:text-neutral-400">
              {{ mappedCount }} of {{ totalCount }} mapped
            </span>
            <span
              :class="mappingProgress === 100 ? 'text-green-600 dark:text-green-400' : 'text-neutral-400'"
              class="font-medium"
            >
              {{ mappingProgress }}%
            </span>
          </div>
          <div class="h-1.5 rounded-full bg-neutral-100 dark:bg-neutral-800 overflow-hidden">
            <div
              class="h-full rounded-full transition-all duration-500"
              :class="mappingProgress === 100 ? 'bg-green-500' : 'bg-neutral-800 dark:bg-neutral-200'"
              :style="{ width: `${mappingProgress}%` }"
            />
          </div>
        </div>

        <!-- Toolbar: filter tabs + search -->
        <div class="flex flex-col sm:flex-row items-stretch sm:items-center gap-2">
          <div class="flex items-center gap-1 rounded-lg bg-neutral-100 dark:bg-neutral-800 p-0.5 shrink-0">
            <button
              v-for="tab in ([
                { key: 'all', label: 'All', count: totalCount },
                { key: 'unmapped', label: 'Unmapped', count: unmappedCount },
                { key: 'mapped', label: 'Mapped', count: mappedCount },
              ] as const)"
              :key="tab.key"
              type="button"
              class="cursor-pointer px-2.5 py-1 text-xs font-medium rounded-md transition-colors"
              :class="activeFilter === tab.key
                ? 'bg-white dark:bg-neutral-700 text-neutral-900 dark:text-neutral-100 shadow-sm'
                : 'text-neutral-500 dark:text-neutral-400 hover:text-neutral-700 dark:hover:text-neutral-300'"
              @click="activeFilter = tab.key"
            >
              {{ tab.label }}
              <span
                class="ml-1 tabular-nums"
                :class="activeFilter === tab.key ? 'text-neutral-500 dark:text-neutral-400' : 'text-neutral-400 dark:text-neutral-500'"
              >{{ tab.count }}</span>
            </button>
          </div>
          <UInput
            v-model="searchQuery"
            placeholder="Filter projects..."
            icon="i-lucide-search"
            size="sm"
            class="flex-1"
            :ui="{ trailing: 'pr-1' }"
          />
        </div>

        <!-- Project list -->
        <div class="rounded-xl border border-neutral-200 dark:border-neutral-700 overflow-hidden">
          <div
            v-if="filteredProjects.length === 0"
            class="p-6 text-center"
          >
            <p class="text-xs text-neutral-400">
              No projects match your filter.
            </p>
          </div>
          <div
            v-else
            class="divide-y divide-neutral-100 dark:divide-neutral-800"
          >
            <div
              v-for="project in paginatedProjects"
              :key="project.id"
              class="flex items-center gap-3 px-4 py-2.5 group"
            >
              <!-- Project name -->
              <div class="flex items-center gap-2 min-w-0 flex-1">
                <UIcon
                  name="i-lucide-folder"
                  class="size-4 shrink-0"
                  :class="project.mapped_repo_id ? 'text-neutral-400' : 'text-amber-500'"
                />
                <TruncatedText
                  :text="project.name"
                  class="text-sm text-neutral-900 dark:text-neutral-100"
                />
              </div>

              <!-- Mapped: show repo chip + source + unmap -->
              <div
                v-if="project.mapped_repo_id"
                class="flex items-center gap-2 shrink-0"
              >
                <div
                  class="flex items-center gap-1.5 rounded-md bg-neutral-100 dark:bg-neutral-800 px-2.5 py-1"
                >
                  <UIcon
                    name="i-lucide-book"
                    class="size-3.5 text-neutral-500"
                  />
                  <TruncatedText
                    :text="repoName(project.mapped_repo_id)"
                    class="text-xs font-medium text-neutral-700 dark:text-neutral-300 max-w-24 sm:max-w-44"
                  />
                </div>
                <UBadge
                  :label="sourceBadgeLabel(project.mapping_method)"
                  :color="sourceBadgeColor(project.mapping_method)"
                  variant="subtle"
                  size="xs"
                  class="hidden sm:inline-flex"
                />
                <button
                  type="button"
                  class="cursor-pointer opacity-0 group-hover:opacity-100 transition-opacity p-0.5 rounded hover:bg-neutral-200 dark:hover:bg-neutral-700"
                  title="Unmap"
                  @click="handleUnmap(project)"
                >
                  <UIcon
                    name="i-lucide-x"
                    class="size-3.5 text-neutral-400 hover:text-neutral-600 dark:hover:text-neutral-300"
                  />
                </button>
              </div>

              <!-- Unmapped: org picker → repo search -->
              <div
                v-else
                class="shrink-0"
              >
                <RepoMapPicker
                  :git-orgs="availableGitOrgs"
                  @select="handleRepoChange(project, $event)"
                />
              </div>
            </div>
          </div>
        </div>

        <!-- Pagination -->
        <div
          v-if="totalPages > 1"
          class="flex items-center justify-between pt-1"
        >
          <p class="text-xs text-neutral-400 tabular-nums">
            {{ (currentPage - 1) * PAGE_SIZE + 1 }}–{{ Math.min(currentPage * PAGE_SIZE, filteredProjects.length) }}
            of {{ filteredProjects.length }}
          </p>
          <div class="flex items-center gap-1">
            <UButton
              icon="i-lucide-chevron-left"
              variant="ghost"
              color="neutral"
              size="xs"
              :disabled="currentPage <= 1"
              @click="currentPage--"
            />
            <span class="text-xs text-neutral-500 tabular-nums px-1">
              {{ currentPage }} / {{ totalPages }}
            </span>
            <UButton
              icon="i-lucide-chevron-right"
              variant="ghost"
              color="neutral"
              size="xs"
              :disabled="currentPage >= totalPages"
              @click="currentPage++"
            />
          </div>
        </div>
      </div>
    </section>

    <!-- Triggers -->
    <section v-if="settings">
      <h4 class="text-sm font-semibold text-neutral-900 dark:text-neutral-100 mb-3">
        Triggers
      </h4>
      <div class="rounded-xl border border-neutral-200 dark:border-neutral-700 divide-y divide-neutral-100 dark:divide-neutral-800 overflow-hidden">
        <div class="px-4 py-3.5 space-y-2">
          <div class="flex items-center justify-between">
            <div class="flex items-center gap-1.5">
              <p class="text-sm text-neutral-900 dark:text-neutral-100">
                Issue auto-fix
              </p>
              <UTooltip :text="triageHints[triageValue]">
                <UIcon
                  name="i-lucide-info"
                  class="size-3.5 text-neutral-400 hover:text-neutral-600 dark:hover:text-neutral-300 cursor-help transition-colors"
                />
              </UTooltip>
            </div>
            <USelect
              :model-value="triageValue"
              :items="triageOptions"
              value-key="value"
              size="sm"
              class="w-48 sm:w-52"
              @update:model-value="updateTriage($event as string)"
            />
          </div>
        </div>
        <div
          v-if="settings.triggers.triage === 'automatic'"
          class="px-4 py-3.5 space-y-2"
        >
          <div class="flex items-center justify-between">
            <div class="flex items-center gap-1.5">
              <p class="text-sm text-neutral-900 dark:text-neutral-100">
                Batch window
              </p>
              <UTooltip text="How long to wait between dispatch batches — a longer window lets more related errors land in one PR.">
                <UIcon
                  name="i-lucide-info"
                  class="size-3.5 text-neutral-400 hover:text-neutral-600 dark:hover:text-neutral-300 cursor-help transition-colors"
                />
              </UTooltip>
            </div>
            <USelect
              :model-value="settings.batch_window"
              :items="batchWindowOptions"
              value-key="value"
              size="sm"
              class="w-48 sm:w-52"
              @update:model-value="updateBatchWindow($event as string)"
            />
          </div>
        </div>
        <div
          v-if="settings.triggers.triage === 'automatic'"
          class="px-4 py-3.5 space-y-2"
        >
          <div class="flex items-center justify-between">
            <div class="flex items-center gap-1.5">
              <p class="text-sm text-neutral-900 dark:text-neutral-100">
                Batch size
              </p>
              <UTooltip text="How many issues a single dispatch batch may carry for this org.">
                <UIcon
                  name="i-lucide-info"
                  class="size-3.5 text-neutral-400 hover:text-neutral-600 dark:hover:text-neutral-300 cursor-help transition-colors"
                />
              </UTooltip>
            </div>
            <USelect
              :model-value="batchSizeValue"
              :items="batchSizeOptions"
              value-key="value"
              size="sm"
              class="w-48 sm:w-52"
              @update:model-value="updateBatchSize($event as string)"
            />
          </div>
        </div>
        <div
          v-if="settings.triggers.triage === 'automatic'"
          class="px-4 py-3.5 space-y-2"
        >
          <div class="flex items-center justify-between">
            <div class="flex items-center gap-1.5">
              <p class="text-sm text-neutral-900 dark:text-neutral-100">
                Wait for all PRs to be merged or closed
              </p>
              <UTooltip text="Don't start the next batch until every PR the previous batch opened has been merged or closed.">
                <UIcon
                  name="i-lucide-info"
                  class="size-3.5 text-neutral-400 hover:text-neutral-600 dark:hover:text-neutral-300 cursor-help transition-colors"
                />
              </UTooltip>
            </div>
            <USwitch
              :model-value="gateOnOpenFixPrs"
              size="sm"
              class="mt-0.5 shrink-0"
              @update:model-value="updateGateOnOpenFixPrs($event)"
            />
          </div>
        </div>
      </div>
    </section>

    <!-- Issue import -->
    <section v-if="settings">
      <h4 class="text-sm font-semibold text-neutral-900 dark:text-neutral-100 mb-3">
        Issue import
      </h4>
      <div class="rounded-xl border border-neutral-200 dark:border-neutral-700 overflow-hidden">
        <div class="px-4 py-3.5 space-y-2">
          <div class="flex items-center justify-between gap-3">
            <div class="min-w-0">
              <p class="text-sm text-neutral-900 dark:text-neutral-100">
                {{ $t('onboarding.sentry.backfillLabel') }}
              </p>
              <p class="text-xs text-neutral-500 dark:text-neutral-400 mt-0.5">
                {{ scopeHint }}
              </p>
            </div>
            <div class="flex items-center gap-2 shrink-0">
              <USelect
                :model-value="currentScope"
                :items="scopeOptions"
                value-key="value"
                size="sm"
                class="w-40 sm:w-44"
                @update:model-value="updateBackfillScope($event as string)"
              />
              <UButton
                :label="$t('onboarding.sentry.backfillNow')"
                :loading="isImporting"
                variant="outline"
                color="neutral"
                icon="i-lucide-download"
                size="xs"
                @click="handleImportNow"
              />
            </div>
          </div>
        </div>
      </div>
    </section>

    <DangerZone
      title="Disconnect organization"
      description="Remove this Sentry organization, projects, and all mappings."
      :loading="isDeleting"
      @confirm="handleDelete"
    />
  </div>
</template>

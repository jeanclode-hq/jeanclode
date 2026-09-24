<script setup lang="ts">
import type { TableColumn } from '@nuxt/ui'
import type { Issue, IssueFilters } from '~/types/api'

const { t } = useI18n()
const route = useRoute()
const router = useRouter()

useHead({ title: () => `${t('issues.title')} - Jeanclode` })

const { user: authUser } = useAuth()
const workspaceId = useActiveWorkspaceId()
const { data: stats, status: statsStatus } = useWorkspaceStatsQuery(workspaceId)
const { data: sources } = useWorkspaceSourcesQuery(workspaceId)
const statsLoading = computed(() => statsStatus.value === 'pending')
const mergeRateHint = computed(() => {
  const created = stats.value?.issues.prs_created ?? 0
  if (!created) return t('issues.stats.noneOpened')
  return t('issues.stats.mergeRate', { rate: Math.round(((stats.value?.issues.prs_merged ?? 0) / created) * 100) })
})

// ---------------------------------------------------------------------------
// URL-synced filters
// ---------------------------------------------------------------------------
const currentPage = ref(Number(route.query.page) || 1)
const limit = ref(25)
const statusFilter = ref((route.query.status as string) || 'open')
const searchQuery = ref((route.query.search as string) || '')
const selectedOrgId = ref((route.query.org as string) || '')
const authorFilter = ref<'all' | 'mine'>((route.query.author as 'all' | 'mine') || 'all')
const dateFilter = ref((route.query.date as string) || '7days')
const repositoryFilter = ref((route.query.repo as string) || '')
const mappedOnlyFilter = ref(route.query.mapped === '1')
const executionStatusFilter = ref((route.query.exec_status as string) || 'all')

const {
  search: repoSearch,
  repos: repoRepos,
  hasMore: repoHasMore,
  isLoading: repoIsLoading,
  loadMore: repoLoadMore,
} = useIssueRepositoriesInfinite(workspaceId, selectedOrgId)

function updateUrl() {
  const q: Record<string, string> = {}
  if (statusFilter.value !== 'open') q.status = statusFilter.value
  if (dateFilter.value !== '7days') q.date = dateFilter.value
  if (searchQuery.value) q.search = searchQuery.value
  if (currentPage.value > 1) q.page = String(currentPage.value)
  if (selectedOrgId.value) q.org = selectedOrgId.value
  if (repositoryFilter.value) q.repo = repositoryFilter.value
  if (mappedOnlyFilter.value) q.mapped = '1'
  if (executionStatusFilter.value !== 'all') q.exec_status = executionStatusFilter.value
  router.replace({ query: q })
}

let debounce: ReturnType<typeof setTimeout> | undefined
watch(searchQuery, () => {
  clearTimeout(debounce)
  debounce = setTimeout(() => {
    currentPage.value = 1
    updateUrl()
  }, 300)
})
watch(selectedOrgId, () => {
  repositoryFilter.value = ''
})
watch([statusFilter, dateFilter, selectedOrgId, authorFilter, repositoryFilter, mappedOnlyFilter, executionStatusFilter], () => {
  currentPage.value = 1
  updateUrl()
})
watch(currentPage, updateUrl)

const apiStatus = computed(() => {
  return statusFilter.value !== 'all' ? statusFilter.value : undefined
})

// ---------------------------------------------------------------------------
// Data
// ---------------------------------------------------------------------------
const filters = computed<IssueFilters>(() => ({
  workspace_id: workspaceId.value,
  page: currentPage.value,
  limit: limit.value,
  status: apiStatus.value,
  search: searchQuery.value || undefined,
  source_org_id: selectedOrgId.value || undefined,
  repository_id: repositoryFilter.value || undefined,
  author: authorFilter.value === 'mine' ? authUser.value?.display_username : undefined,
  period: dateFilter.value !== 'all' ? dateFilter.value : undefined,
  mapped_only: mappedOnlyFilter.value || undefined,
  execution_status: executionStatusFilter.value !== 'all' ? executionStatusFilter.value : undefined,
}))

const { data, status: queryStatus } = useIssuesQuery(filters)
const { mutateAsync: triggerFix } = useManualExecutionMutation()
const { mutateAsync: triggerResolve } = useResolveIssueMutation()
const { mutateAsync: cancelExecution } = useCancelExecutionMutation()

const pendingFixes = reactive(new Set<string>())

function handleFix(issue: Issue) {
  if (pendingFixes.has(issue.id)) return
  pendingFixes.add(issue.id)
  const trigger = isGitIssue(issue.source) ? triggerResolve : triggerFix
  trigger(issue.id).finally(() => pendingFixes.delete(issue.id))
}

const cancellingExecutionId = ref<string | null>(null)

function handleCancel(executionId: string) {
  if (cancellingExecutionId.value) return
  cancellingExecutionId.value = executionId
  cancelExecution(executionId).finally(() => {
    cancellingExecutionId.value = null
  })
}

const issues = computed(() => data.value?.objects ?? [])
const total = computed(() => data.value?.pagination?.total ?? 0)
const loading = computed(() => queryStatus.value === 'pending')
const connectedSources = computed(() => sources.value?.sources ?? [])

// Status helpers from ~/utils/status (auto-imported)

const statusOptions = computed(() => [
  { label: t('issues.filters.allStatuses'), value: 'all' },
  { label: 'Open', value: 'open' },
  { label: 'Closed', value: 'closed' },
])
const dateOptions = computed(() => [
  { label: 'All time', value: 'all' },
  { label: 'Today', value: 'today' },
  { label: 'Last 7 days', value: '7days' },
  { label: 'Last 30 days', value: '30days' },
])
const executionStatusOptions = computed(() => [
  { label: t('issues.filters.executionStatus.all'), value: 'all' },
  { label: t('issues.filters.executionStatus.pending'), value: 'pending' },
  { label: t('issues.filters.executionStatus.running'), value: 'running' },
  { label: t('issues.filters.executionStatus.completed'), value: 'completed' },
  { label: t('issues.filters.executionStatus.failed'), value: 'failed' },
])
// Disambiguate repos sharing a display name (e.g. a Sentry project and its
// mapped git repo) by appending the org name — best-effort across the
// currently loaded page(s), since the full repo set is paginated server-side.
const repoFilterOptions = computed(() => {
  // Names are shortened first so the count reflects what the user actually
  // sees — two orgs can each hold a `atlas/api` once the root group is gone.
  const repos = repoRepos.value.map((repo) => ({ ...repo, name: shortRepoName(repo.name) }))
  const nameCounts = repos.reduce<Record<string, number>>((acc, repo) => {
    acc[repo.name] = (acc[repo.name] ?? 0) + 1
    return acc
  }, {})
  return repos.map((repo) => ({
    ...repo,
    name: nameCounts[repo.name]! > 1 ? `${repo.name} (${repo.org_name})` : repo.name,
  }))
})

const columns = computed<TableColumn<Issue>[]>(() => [
  { accessorKey: 'title', header: t('issues.columns.title') },
  { accessorKey: 'source', header: t('issues.columns.source') },
  { accessorKey: 'project', header: 'Project' },
  { accessorKey: 'execution_status', header: t('issues.columns.status') },
  { accessorKey: 'first_seen', header: 'Date' },
  { id: 'actions', header: '' },
])

function isActionable(status: string): boolean {
  return status === 'unresolved' || status === 'open'
}

function displayStatus(issue: Issue): string {
  if (pendingFixes.has(issue.id) && issue.execution_status === 'pending')
    return 'queued'
  return issue.execution_status
}

function isProcessing(issue: Issue): boolean {
  const s = displayStatus(issue)
  return s === 'queued' || s === 'running'
}

function outcomeStatus(issue: Issue): string {
  return isProcessing(issue) ? displayStatus(issue) : issue.result
}

const detailModalOpen = ref(false)
const selectedIssue = ref<Issue | null>(null)

function openDetail(issue: Issue) {
  selectedIssue.value = issue
  detailModalOpen.value = true
}

const pgStart = computed(() => total.value === 0 ? 0 : (currentPage.value - 1) * limit.value + 1)
const pgEnd = computed(() => Math.min(currentPage.value * limit.value, total.value))

function handleLimitChange(newLimit: number) {
  limit.value = newLimit
  currentPage.value = 1
}
</script>

<template>
  <div class="flex flex-col h-[calc(100vh-3.5rem-3rem)]">
    <div class="shrink-0 space-y-4 pb-4">
      <!-- Stats -->
      <StatStrip :columns="3">
        <StatTile
          icon="i-lucide-circle-check"
          :label="$t('issues.stats.handled')"
          :value="stats?.issues.handled"
          :hint="$t('issues.stats.handledHint')"
          :loading="statsLoading"
        />
        <StatTile
          icon="i-lucide-git-pull-request"
          :label="$t('issues.stats.prsOpened')"
          :value="stats?.issues.prs_created"
          :hint="$t('issues.stats.prsOpenedHint')"
          :loading="statsLoading"
        />
        <StatTile
          icon="i-lucide-git-merge"
          :label="$t('issues.stats.prsMerged')"
          :value="stats?.issues.prs_merged"
          :hint="mergeRateHint"
          :loading="statsLoading"
        />
      </StatStrip>

      <!-- Source tabs -->
      <SourceTabs
        v-model="selectedOrgId"
        data-guide="issueSources"
        :sources="connectedSources"
      />

      <!-- Filters -->
      <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-3">
        <div class="flex items-center gap-3 shrink-0">
          <div class="flex items-center gap-2">
            <span
              class="text-xs font-medium transition-colors"
              :class="authorFilter === 'mine' ? 'text-neutral-900 dark:text-neutral-100' : 'text-neutral-400 dark:text-neutral-500'"
            >{{ $t('issues.filters.myIssues') }}</span>
            <USwitch
              :model-value="authorFilter === 'all'"
              size="xs"
              @update:model-value="authorFilter = $event ? 'all' : 'mine'"
            />
            <span
              class="text-xs font-medium transition-colors"
              :class="authorFilter === 'all' ? 'text-neutral-900 dark:text-neutral-100' : 'text-neutral-400 dark:text-neutral-500'"
            >{{ $t('issues.filters.allIssues') }}</span>
          </div>
          <div
            data-guide="issueMapped"
            class="flex items-center gap-2"
          >
            <span class="text-xs font-medium text-neutral-500 dark:text-neutral-400">{{ $t('issues.filters.mappedReposOnly') }}</span>
            <USwitch
              v-model="mappedOnlyFilter"
              size="xs"
            />
          </div>
          <UInput
            v-model="searchQuery"
            icon="i-lucide-search"
            :placeholder="$t('common.search')"
            size="sm"
            class="min-w-0 w-48 shrink"
          />
        </div>
        <div class="flex items-center gap-2 min-w-0">
          <RepoFilterMenu
            v-model="repositoryFilter"
            v-model:search="repoSearch"
            :repos="repoFilterOptions"
            :has-more="repoHasMore"
            :is-loading="repoIsLoading"
            :load-more="repoLoadMore"
            :placeholder="$t('issues.filters.allRepos')"
          />
          <USelect
            v-model="dateFilter"
            :items="dateOptions"
            value-key="value"
            size="sm"
            class="min-w-0 w-36 shrink"
          />
          <USelect
            v-model="statusFilter"
            :items="statusOptions"
            value-key="value"
            size="sm"
            class="min-w-0 w-32 shrink"
          />
          <USelect
            v-model="executionStatusFilter"
            :items="executionStatusOptions"
            value-key="value"
            size="sm"
            class="min-w-0 w-36 shrink"
          />
        </div>
      </div>
    </div>

    <!-- Table -->
    <Transition
      mode="out-in"
      enter-active-class="transition-all duration-200 ease-out"
      enter-from-class="opacity-0 translate-y-1"
      enter-to-class="opacity-100 translate-y-0"
      leave-active-class="transition-all duration-150 ease-in"
      leave-from-class="opacity-100 translate-y-0"
      leave-to-class="opacity-0 -translate-y-1"
    >
      <div
        :key="selectedOrgId + statusFilter + repositoryFilter + mappedOnlyFilter + executionStatusFilter"
        class="flex-1 min-h-0 flex flex-col rounded-xl border border-neutral-200 dark:border-neutral-700 bg-white dark:bg-neutral-800 shadow-sm overflow-hidden"
      >
        <!-- Desktop -->
        <div
          v-if="issues.length > 0 || loading"
          data-guide="issueList"
          class="hidden lg:flex lg:flex-col lg:flex-1 lg:min-h-0 overflow-hidden"
        >
          <UTable
            :data="issues"
            :columns="columns"
            sticky
            :loading="loading"
            class="flex-1 min-h-0"
            :on-select="(_e: unknown, row: { original: Issue }) => openDetail(row.original)"
            :ui="{
              root: 'flex-1 overflow-y-auto overflow-x-hidden',
              base: 'w-full table-auto',
              thead: 'bg-neutral-50 dark:bg-neutral-700 border-b border-neutral-200 dark:border-neutral-600',
              th: 'px-4 py-2 text-xs font-semibold text-neutral-500 dark:text-neutral-300 uppercase tracking-wider whitespace-nowrap first:w-full [&:nth-child(2)]:w-16 [&:nth-child(3)]:w-36 [&:nth-child(4)]:w-32 [&:nth-child(5)]:w-32 [&:nth-child(6)]:w-28',
              tbody: 'divide-y divide-neutral-100 dark:divide-neutral-700',
              tr: 'group hover:bg-neutral-50 dark:hover:bg-neutral-800/50 transition-colors duration-150 cursor-pointer',
              td: 'px-4 py-2.5 text-sm whitespace-nowrap first:max-w-0',
            }"
          >
            <template #title-cell="{ row }">
              <div
                class="flex items-center gap-2 min-w-0 cursor-pointer"
                @click.stop="row.original.issue_url && navigateTo(row.original.issue_url, { external: true, open: { target: '_blank' } })"
              >
                <UTooltip
                  :text="row.original.title"
                  :delay-duration="300"
                  class="min-w-0 flex-1"
                >
                  <span class="font-medium text-neutral-900 dark:text-neutral-100 hover:text-neutral-700 dark:hover:text-neutral-300 transition-colors block truncate">{{ row.original.title }}</span>
                </UTooltip>
                <UBadge
                  :color="(getSourceStatusColor(row.original.status) as any)"
                  variant="soft"
                  size="xs"
                  class="flex-shrink-0 capitalize"
                >
                  {{ getSourceStatusLabel(row.original.status) }}
                </UBadge>
              </div>
            </template>
            <template #source-cell="{ row }">
              <span class="text-neutral-500 dark:text-neutral-400 whitespace-nowrap capitalize">{{ row.original.source }}</span>
            </template>
            <template #project-cell="{ row }">
              <TruncatedText
                :text="shortRepoName(row.original.project)"
                class="text-neutral-500 dark:text-neutral-400 max-w-32"
              />
            </template>
            <template #execution_status-cell="{ row }">
              <ExecutionBadge
                data-guide="issueList"
                :status="outcomeStatus(row.original)"
                :workflow="row.original.workflow ?? undefined"
              />
            </template>
            <template #first_seen-cell="{ row }">
              <span class="text-neutral-500 dark:text-neutral-400 whitespace-nowrap">{{ timeAgo(row.original.first_seen) }}</span>
            </template>
            <template #actions-cell="{ row }">
              <div
                data-guide="issueFix"
                class="text-right"
                @click.stop
              >
                <UButton
                  v-if="!row.original.has_mapping"
                  color="warning"
                  variant="subtle"
                  size="xs"
                  icon="i-lucide-link"
                  class="uppercase tracking-wide min-w-22 justify-center"
                  :to="{ name: 'integrations', query: { tab: row.original.source, org: row.original.source_org_id } }"
                >
                  Map
                </UButton>
                <UTooltip
                  v-else-if="!row.original.repo_enabled"
                  :text="$t('issues.actions.repoDisabledTooltip')"
                >
                  <AppButton
                    disabled
                    color="neutral"
                    size="xs"
                    icon="i-lucide-ban"
                    class="uppercase tracking-wide min-w-22 justify-center"
                  >
                    {{ isGitIssue(row.original.source) ? $t('issues.actions.resolve') : $t('issues.actions.fix') }}
                  </AppButton>
                </UTooltip>
                <ExecutionRowButton
                  v-else-if="isProcessing(row.original)"
                  :status="(displayStatus(row.original) as 'queued' | 'running')"
                  :label="isGitIssue(row.original.source) ? $t('issues.actions.resolve') : $t('issues.actions.fix')"
                  :cancelling="!!row.original.execution_id && cancellingExecutionId === row.original.execution_id"
                  @cancel="row.original.execution_id && handleCancel(row.original.execution_id)"
                />
                <AppButton
                  v-else-if="isActionable(row.original.status)"
                  color="primary"
                  size="xs"
                  icon="i-lucide-eye"
                  class="uppercase tracking-wide min-w-22 justify-center"
                  @click="handleFix(row.original)"
                >
                  {{ isGitIssue(row.original.source) ? $t('issues.actions.resolve') : $t('issues.actions.fix') }}
                </AppButton>
                <AppButton
                  v-else
                  disabled
                  color="neutral"
                  size="xs"
                  icon="i-lucide-eye-off"
                  class="uppercase tracking-wide min-w-22 justify-center"
                >
                  FIX
                </AppButton>
              </div>
            </template>
          </UTable>
          <TablePagination
            :page="currentPage"
            :limit="limit"
            :total="total"
            :start="pgStart"
            :end="pgEnd"
            @update:page="currentPage = $event"
            @update:limit="handleLimitChange"
          />
        </div>

        <!-- Mobile -->
        <div
          v-if="issues.length > 0 || loading"
          class="lg:hidden flex-1 overflow-y-auto p-3 space-y-2"
        >
          <div
            v-for="issue in issues"
            :key="issue.id"
            class="rounded-lg border border-neutral-200 dark:border-neutral-700 bg-white dark:bg-neutral-800 p-3 cursor-pointer"
            @click="openDetail(issue)"
          >
            <div class="flex items-start justify-between gap-2">
              <p
                class="text-sm font-medium text-neutral-900 dark:text-neutral-100 line-clamp-2 min-w-0 flex-1"
                @click.stop="issue.issue_url && navigateTo(issue.issue_url, { external: true, open: { target: '_blank' } })"
              >
                {{ issue.title }}
              </p>
              <UBadge
                :color="(getSourceStatusColor(issue.status) as any)"
                variant="soft"
                size="xs"
                class="shrink-0 capitalize"
              >
                {{ getSourceStatusLabel(issue.status) }}
              </UBadge>
            </div>
            <div class="flex items-center justify-between mt-2">
              <div class="flex items-center gap-2 text-xs text-neutral-500 dark:text-neutral-400">
                <ExecutionBadge
                  :status="outcomeStatus(issue)"
                  :workflow="issue.workflow ?? undefined"
                />
                <span>{{ timeAgo(issue.first_seen) }}</span>
              </div>
              <div @click.stop>
                <UButton
                  v-if="!issue.has_mapping"
                  color="warning"
                  variant="subtle"
                  size="xs"
                  icon="i-lucide-link"
                  class="uppercase tracking-wide"
                  :to="{ name: 'integrations', query: { tab: issue.source, org: issue.source_org_id } }"
                >
                  Map
                </UButton>
                <UTooltip
                  v-else-if="!issue.repo_enabled"
                  :text="$t('issues.actions.repoDisabledTooltip')"
                >
                  <AppButton
                    disabled
                    color="neutral"
                    size="xs"
                    icon="i-lucide-ban"
                    class="uppercase tracking-wide"
                  >
                    {{ isGitIssue(issue.source) ? $t('issues.actions.resolve') : $t('issues.actions.fix') }}
                  </AppButton>
                </UTooltip>
                <AppButton
                  v-else-if="isActionable(issue.status) || isProcessing(issue)"
                  :processing="isProcessing(issue)"
                  color="primary"
                  size="xs"
                  icon="i-lucide-eye"
                  class="uppercase tracking-wide"
                  @click="handleFix(issue)"
                >
                  {{ isGitIssue(issue.source) ? $t('issues.actions.resolve') : $t('issues.actions.fix') }}
                </AppButton>
              </div>
            </div>
          </div>
          <TablePagination
            :page="currentPage"
            :limit="limit"
            :total="total"
            :start="pgStart"
            :end="pgEnd"
            @update:page="currentPage = $event"
            @update:limit="handleLimitChange"
          />
        </div>

        <!-- Empty -->
        <div
          v-if="!loading && issues.length === 0"
          class="flex-1 flex flex-col items-center justify-center"
        >
          <div class="size-14 flex items-center justify-center rounded-2xl bg-neutral-100 dark:bg-neutral-800 mb-4">
            <UIcon
              name="i-lucide-inbox"
              class="size-7 text-neutral-400 dark:text-neutral-500"
            />
          </div>
          <p class="text-sm font-medium text-neutral-700 dark:text-neutral-200">
            {{ $t('issues.empty') }}
          </p>
          <p class="text-xs text-neutral-400 dark:text-neutral-500 mt-1.5 max-w-sm text-center">
            {{ $t('issues.emptyDescription') }}
          </p>
        </div>
      </div>
    </Transition>

    <IssueExecutionModal
      v-model:open="detailModalOpen"
      :issue="selectedIssue"
      :retrying="selectedIssue ? isProcessing(selectedIssue) : false"
      :retryable="selectedIssue ? isActionable(selectedIssue.status) : false"
      :cancelling="!!cancellingExecutionId"
      @retry="handleFix"
      @cancel="handleCancel"
    />
  </div>
</template>

<style scoped>
.stat-card {
  animation: fade-up 0.3s ease-out both;
}
.stat-card:nth-child(1) { animation-delay: 0ms; }
.stat-card:nth-child(2) { animation-delay: 75ms; }
.stat-card:nth-child(3) { animation-delay: 150ms; }

@keyframes fade-up {
  from { opacity: 0; transform: translateY(8px); }
  to { opacity: 1; transform: translateY(0); }
}
</style>

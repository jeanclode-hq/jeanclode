<script setup lang="ts">
import type { TableColumn } from '@nuxt/ui'
import type { PRFilters, PRStatus, PullRequest } from '~/types/api'

const { t } = useI18n()
const route = useRoute()
const router = useRouter()
const workspaceStore = useWorkspaceStore()

useHead({ title: () => `${t('pullRequests.title')} - Jeanclode` })

const { user: authUser } = useAuth()
const workspaceId = computed(() => workspaceStore.currentWorkspace?.id)
const { data: stats, status: statsStatus } = useWorkspaceStatsQuery(workspaceId)
const statsLoading = computed(() => statsStatus.value === 'pending')
const { data: sources } = useWorkspaceSourcesQuery(workspaceId)

const pendingReviews = reactive(new Set<string>())
const { mutateAsync: triggerReview } = useTriggerPrWorkflowMutation()
const { mutateAsync: cancelExecution } = useCancelExecutionMutation()

function isPRAuthor(pr: PullRequest): boolean {
  const u = authUser.value
  if (!u) return false
  if (pr.provider === 'github') return !!u.github_username && pr.author === u.github_username
  if (pr.provider === 'gitlab') return !!u.gitlab_username && pr.author === u.gitlab_username
  return false
}

// Mirrors issues.vue/displayStatus — once we've optimistically queued
// the review, treat the row as "queued" until the SSE update lands so
// the AppButton shimmer kicks in immediately.
function reviewDisplayStatus(pr: PullRequest): PullRequest['execution_status'] {
  if (
    pendingReviews.has(pr.id)
    && (pr.execution_status === 'none' || pr.execution_status === 'pending')
  ) {
    return 'queued'
  }
  return pr.execution_status
}

function isReviewProcessing(pr: PullRequest): boolean {
  const s = reviewDisplayStatus(pr)
  return s === 'queued' || s === 'running'
}

function handleReview(pr: PullRequest) {
  if (pendingReviews.has(pr.id)) return
  pendingReviews.add(pr.id)
  triggerReview({ prId: pr.id, workflow: 'review' }).finally(() => pendingReviews.delete(pr.id))
}

const cancellingExecutionId = ref<string | null>(null)

// RUNNING only — a QUEUED execution has no container yet, and cancelling it
// would race the consumer that's about to dispatch it (see the backend's
// executions/route.py).
function canCancelPr(pr: PullRequest): boolean {
  return !!pr.execution_id && reviewDisplayStatus(pr) === 'running'
}

function handleCancel(pr: PullRequest) {
  if (!pr.execution_id || cancellingExecutionId.value) return
  cancellingExecutionId.value = pr.execution_id
  cancelExecution(pr.execution_id).finally(() => {
    cancellingExecutionId.value = null
  })
}

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
const executionStatusFilter = ref((route.query.exec_status as string) || 'all')

const {
  search: repoSearch,
  repos: repoRepos,
  hasMore: repoHasMore,
  isLoading: repoIsLoading,
  loadMore: repoLoadMore,
} = usePrRepositoriesInfinite(workspaceId, selectedOrgId)

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

function updateUrl() {
  const q: Record<string, string> = {}
  if (statusFilter.value !== 'open') q.status = statusFilter.value
  if (dateFilter.value !== '7days') q.date = dateFilter.value
  if (searchQuery.value) q.search = searchQuery.value
  if (currentPage.value > 1) q.page = String(currentPage.value)
  if (repositoryFilter.value) q.repo = repositoryFilter.value
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
watch([statusFilter, dateFilter, selectedOrgId, authorFilter, repositoryFilter, executionStatusFilter], () => {
  currentPage.value = 1
  updateUrl()
})
watch(currentPage, updateUrl)

// ---------------------------------------------------------------------------
// Data
// ---------------------------------------------------------------------------
const filters = computed<PRFilters>(() => ({
  page: currentPage.value,
  limit: limit.value,
  status: statusFilter.value !== 'all' ? statusFilter.value as PRStatus : undefined,
  search: searchQuery.value || undefined,
  org_id: selectedOrgId.value || undefined,
  repository_id: repositoryFilter.value || undefined,
  author: authorFilter.value === 'mine' ? authUser.value?.display_username : undefined,
  period: dateFilter.value !== 'all' ? dateFilter.value : undefined,
  execution_status: executionStatusFilter.value !== 'all' ? executionStatusFilter.value : undefined,
}))

const { data, status: queryStatus } = usePullRequestsQuery(workspaceId, filters)

const prs = computed(() => data.value?.objects ?? [])
const total = computed(() => data.value?.pagination?.total ?? 0)
const loading = computed(() => queryStatus.value === 'pending')
const totalPrs = computed(() => (stats.value?.pr_open ?? 0) + (stats.value?.pr_merged ?? 0))
const connectedSources = computed(() =>
  (sources.value?.sources ?? []).filter((s) => s.provider === 'github' || s.provider === 'gitlab'),
)

function openPR(url: string) {
  navigateTo(url, { external: true, open: { target: '_blank' } })
}

const detailModalOpen = ref(false)
const selectedPr = ref<PullRequest | null>(null)

function openDetail(pr: PullRequest) {
  selectedPr.value = pr
  detailModalOpen.value = true
}

const selectedPrSubtitle = computed(() => selectedPr.value?.repo_name ?? null)

const statusOptions = computed(() => [
  { label: t('pullRequests.filters.allStatuses'), value: 'all' },
  { label: 'Open', value: 'open' },
  { label: 'Merged', value: 'merged' },
  { label: 'Closed', value: 'closed' },
])
const dateOptions = computed(() => [
  { label: t('pullRequests.filters.allTime'), value: 'all' },
  { label: t('pullRequests.filters.today'), value: 'today' },
  { label: t('pullRequests.filters.last7Days'), value: '7days' },
  { label: t('pullRequests.filters.last30Days'), value: '30days' },
])
const executionStatusOptions = computed(() => [
  { label: t('pullRequests.filters.executionStatus.all'), value: 'all' },
  { label: t('pullRequests.filters.executionStatus.pending'), value: 'pending' },
  { label: t('pullRequests.filters.executionStatus.running'), value: 'running' },
  { label: t('pullRequests.filters.executionStatus.completed'), value: 'completed' },
  { label: t('pullRequests.filters.executionStatus.failed'), value: 'failed' },
])

const columns = computed<TableColumn<PullRequest>[]>(() => [
  { accessorKey: 'title', header: t('pullRequests.columns.title') },
  { accessorKey: 'repo_name', header: t('pullRequests.columns.repo') },
  { accessorKey: 'execution_status', header: t('pullRequests.columns.status') },
  { accessorKey: 'created_at', header: 'Date' },
  { id: 'actions', header: '' },
])

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
      <div class="grid grid-cols-3 gap-3">
        <template v-if="statsLoading">
          <StatCardSkeleton
            v-for="i in 3"
            :key="i"
          />
        </template>
        <template v-else>
          <StatCard
            icon="i-lucide-git-pull-request"
            icon-bg="bg-neutral-100 dark:bg-neutral-700"
            icon-color="text-neutral-500 dark:text-neutral-400"
            :value="totalPrs"
            label="Total"
          />
          <StatCard
            icon="i-lucide-clock"
            icon-bg="bg-amber-50 dark:bg-amber-900/30"
            icon-color="text-amber-500"
            :value="stats?.pr_open ?? 0"
            label="Pending"
          />
          <StatCard
            icon="i-lucide-check-circle"
            icon-bg="bg-emerald-50 dark:bg-emerald-900/30"
            icon-color="text-emerald-500"
            :value="stats?.pr_merged ?? 0"
            label="Reviewed"
          />
        </template>
      </div>

      <!-- Source tabs -->
      <SourceTabs
        v-model="selectedOrgId"
        :sources="connectedSources"
      />

      <!-- Filters -->
      <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-3">
        <div class="flex items-center gap-3 shrink-0">
          <div class="flex items-center gap-2">
            <span
              class="text-xs font-medium transition-colors"
              :class="authorFilter === 'mine' ? 'text-neutral-900 dark:text-neutral-100' : 'text-neutral-400 dark:text-neutral-500'"
            >{{ $t('pullRequests.filters.myPRs') }}</span>
            <USwitch
              :model-value="authorFilter === 'all'"
              size="xs"
              @update:model-value="authorFilter = $event ? 'all' : 'mine'"
            />
            <span
              class="text-xs font-medium transition-colors"
              :class="authorFilter === 'all' ? 'text-neutral-900 dark:text-neutral-100' : 'text-neutral-400 dark:text-neutral-500'"
            >{{ $t('pullRequests.filters.allPRs') }}</span>
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
            :placeholder="$t('pullRequests.filters.allRepos')"
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
        :key="selectedOrgId + statusFilter + repositoryFilter + executionStatusFilter"
        class="flex-1 min-h-0 flex flex-col rounded-xl border border-neutral-200 dark:border-neutral-700 bg-white dark:bg-neutral-800 shadow-sm overflow-hidden"
      >
        <!-- Desktop -->
        <div
          v-if="prs.length > 0 || loading"
          class="hidden lg:flex lg:flex-col lg:flex-1 lg:min-h-0 overflow-hidden"
        >
          <UTable
            :data="prs"
            :columns="columns"
            sticky
            :loading="loading"
            class="flex-1 min-h-0"
            :on-select="(_e: unknown, row: { original: PullRequest }) => openDetail(row.original)"
            :ui="{
              root: 'flex-1 overflow-y-auto overflow-x-hidden',
              base: 'w-full table-auto',
              thead: 'bg-neutral-50 dark:bg-neutral-700 border-b border-neutral-200 dark:border-neutral-600',
              th: 'px-4 py-2 text-xs font-semibold text-neutral-500 dark:text-neutral-300 uppercase tracking-wider whitespace-nowrap first:w-full [&:nth-child(2)]:w-36 [&:nth-child(3)]:w-32 [&:nth-child(4)]:w-32 [&:nth-child(5)]:w-28',
              tbody: 'divide-y divide-neutral-100 dark:divide-neutral-700',
              tr: 'group hover:bg-neutral-50 dark:hover:bg-neutral-800/50 transition-colors duration-150 cursor-pointer',
              td: 'px-4 py-2.5 text-sm whitespace-nowrap first:max-w-0',
            }"
          >
            <template #title-cell="{ row }">
              <div
                class="flex items-center gap-2 min-w-0 cursor-pointer"
                @click.stop="openPR(row.original.pr_url)"
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
            <template #repo_name-cell="{ row }">
              <TruncatedText
                :text="shortRepoName(row.original.repo_name)"
                class="text-neutral-500 dark:text-neutral-400 max-w-32"
              />
            </template>
            <template #execution_status-cell="{ row }">
              <ExecutionBadge
                :status="reviewDisplayStatus(row.original)"
                :workflow="row.original.workflow ?? undefined"
              />
            </template>
            <template #created_at-cell="{ row }">
              <span class="text-neutral-500 dark:text-neutral-400 whitespace-nowrap">{{ timeAgo(row.original.created_at) }}</span>
            </template>
            <template #actions-cell="{ row }">
              <div
                class="text-right"
                @click.stop
              >
                <UTooltip
                  v-if="!row.original.repo_enabled"
                  :text="$t('pullRequests.reviewButton.repoDisabledTooltip')"
                >
                  <AppButton
                    disabled
                    color="neutral"
                    size="xs"
                    icon="i-lucide-ban"
                    class="uppercase tracking-wide min-w-22 justify-center"
                  >
                    {{ $t('pullRequests.reviewButton.label') }}
                  </AppButton>
                </UTooltip>
                <UTooltip
                  v-else-if="!isPRAuthor(row.original)"
                  :text="$t('pullRequests.reviewButton.notAuthorTooltip')"
                >
                  <AppButton
                    disabled
                    color="neutral"
                    size="xs"
                    icon="i-lucide-eye-off"
                    class="uppercase tracking-wide min-w-22 justify-center"
                  >
                    {{ $t('pullRequests.reviewButton.label') }}
                  </AppButton>
                </UTooltip>
                <ExecutionRowButton
                  v-else-if="isReviewProcessing(row.original)"
                  :status="(reviewDisplayStatus(row.original) as 'queued' | 'running')"
                  :label="$t('pullRequests.reviewButton.label')"
                  :cancelling="!!row.original.execution_id && cancellingExecutionId === row.original.execution_id"
                  @cancel="row.original.execution_id && handleCancel(row.original)"
                />
                <AppButton
                  v-else
                  color="primary"
                  size="xs"
                  icon="i-lucide-eye"
                  class="uppercase tracking-wide min-w-22 justify-center"
                  @click="handleReview(row.original)"
                >
                  {{ $t('pullRequests.reviewButton.label') }}
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
          v-if="prs.length > 0 || loading"
          class="lg:hidden flex-1 overflow-y-auto p-3 space-y-2"
        >
          <div
            v-for="pr in prs"
            :key="pr.id"
            class="rounded-lg border border-neutral-200 dark:border-neutral-700 p-3 cursor-pointer"
            @click="openDetail(pr)"
          >
            <div class="flex items-start justify-between gap-2">
              <p
                class="text-sm font-medium text-neutral-900 dark:text-neutral-100 line-clamp-2 min-w-0 flex-1 hover:text-neutral-700 dark:hover:text-neutral-300 transition-colors"
                @click.stop="openPR(pr.pr_url)"
              >
                {{ pr.title }}
              </p>
              <UBadge
                :color="(getSourceStatusColor(pr.status) as any)"
                variant="soft"
                size="xs"
                class="shrink-0 capitalize"
              >
                {{ getSourceStatusLabel(pr.status) }}
              </UBadge>
            </div>
            <div class="flex items-center justify-between mt-2">
              <div class="flex items-center gap-3 text-xs text-neutral-500 dark:text-neutral-400">
                <TruncatedText
                  :text="shortRepoName(pr.repo_name)"
                  class="max-w-24"
                />
                <span>#{{ pr.pr_number }}</span>
                <span>{{ timeAgo(pr.created_at) }}</span>
              </div>
              <div @click.stop>
                <UTooltip
                  v-if="!pr.repo_enabled"
                  :text="$t('pullRequests.reviewButton.repoDisabledTooltip')"
                >
                  <AppButton
                    disabled
                    color="neutral"
                    size="xs"
                    icon="i-lucide-ban"
                    class="uppercase tracking-wide"
                  >
                    {{ $t('pullRequests.reviewButton.label') }}
                  </AppButton>
                </UTooltip>
                <UTooltip
                  v-else-if="!isPRAuthor(pr)"
                  :text="$t('pullRequests.reviewButton.notAuthorTooltip')"
                >
                  <AppButton
                    disabled
                    color="neutral"
                    size="xs"
                    icon="i-lucide-eye-off"
                    class="uppercase tracking-wide"
                  >
                    {{ $t('pullRequests.reviewButton.label') }}
                  </AppButton>
                </UTooltip>
                <AppButton
                  v-else
                  :processing="isReviewProcessing(pr)"
                  color="primary"
                  size="xs"
                  icon="i-lucide-eye"
                  class="uppercase tracking-wide"
                  @click="handleReview(pr)"
                >
                  {{ $t('pullRequests.reviewButton.label') }}
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
          v-if="!loading && prs.length === 0"
          class="flex-1 flex flex-col items-center justify-center"
        >
          <div class="size-14 flex items-center justify-center rounded-2xl bg-neutral-100 dark:bg-neutral-800 mb-4">
            <UIcon
              name="i-lucide-git-pull-request"
              class="size-7 text-neutral-400 dark:text-neutral-500"
            />
          </div>
          <p class="text-sm font-medium text-neutral-700 dark:text-neutral-200">
            {{ $t('pullRequests.empty') }}
          </p>
          <p class="text-xs text-neutral-400 dark:text-neutral-500 mt-1.5 max-w-sm text-center">
            {{ $t('pullRequests.emptyDescription') }}
          </p>
        </div>
      </div>
    </Transition>

    <ExecutionResultModal
      v-if="selectedPr"
      v-model:open="detailModalOpen"
      :title="selectedPr.title"
      :subtitle="selectedPrSubtitle"
      :source="selectedPr.provider"
      :status="reviewDisplayStatus(selectedPr)"
      :workflow="selectedPr.workflow ?? undefined"
      :error-type="selectedPr.error_type"
      :error-detail="selectedPr.error_detail"
      :trigger="selectedPr.execution_trigger"
      :created-at="selectedPr.execution_started_at"
      :external-url="selectedPr.pr_url"
      external-label="View pull request"
      :can-retry="selectedPr.execution_status === 'failed' && selectedPr.repo_enabled && isPRAuthor(selectedPr)"
      :retrying="isReviewProcessing(selectedPr)"
      :can-cancel="canCancelPr(selectedPr)"
      :cancelling="!!cancellingExecutionId"
      @retry="handleReview(selectedPr)"
      @cancel="handleCancel(selectedPr)"
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

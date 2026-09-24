<script setup lang="ts">
import type { WorkspaceExecution } from '~/types/api'

const { t } = useI18n()
const workspaceStore = useWorkspaceStore()
const onboarding = useOnboarding()
const { user: authUser } = useAuth()

const showCreateModal = ref(false)

useHead({
  title: () => `${t('dashboard.title')} - Jeanclode`,
})

const workspaceId = useActiveWorkspaceId()

// Global stats
const { data: stats, status: statsStatus } = useWorkspaceStatsQuery(workspaceId)
const statsLoading = computed(() => statsStatus.value === 'pending')

// Active executions
const { data: activeExecutionsData, status: activeStatus } = useActiveExecutionsQuery(workspaceId)
const activeLoading = computed(() => activeStatus.value === 'pending')
const activeExecutions = computed(() => activeExecutionsData.value ?? [])

const { mutateAsync: cancelExecution } = useCancelExecutionMutation()
const cancellingExecutionId = ref<string | null>(null)

function handleCancel(executionId: string) {
  if (cancellingExecutionId.value) return
  cancellingExecutionId.value = executionId
  cancelExecution(executionId).finally(() => {
    cancellingExecutionId.value = null
  })
}

const detailModalOpen = ref(false)
const selectedExecution = ref<WorkspaceExecution | null>(null)

function openDetail(execution: WorkspaceExecution) {
  selectedExecution.value = execution
  detailModalOpen.value = true
}

const dashboardStats = computed(() => stats.value?.dashboard)
const windowHint = computed(() => t('dashboard.stats.window', { days: stats.value?.window_days ?? 30 }))

const greeting = computed(() => {
  const name = authUser.value?.display_username
  return name ? `${t('dashboard.greeting')}, ${name}.` : `${t('dashboard.greeting')}.`
})
</script>

<template>
  <div class="flex flex-col gap-6">
    <!-- No workspace -->
    <DashboardBanner
      v-if="!workspaceStore.currentWorkspace && !workspaceStore.loading"
      icon="i-lucide-building-2"
      title="You're not part of any workspace"
      description="Create a new workspace or ask your team admin to invite you to an existing one."
      action-label="Create workspace"
      @action="showCreateModal = true"
    />

    <!-- Onboarding incomplete -->
    <DashboardBanner
      v-else-if="onboarding.needsOnboarding.value && workspaceStore.currentWorkspace"
      icon="i-lucide-rocket"
      title="Finish setting up your workspace"
      description="Connect your git provider and integrations to unlock all features."
      action-label="Continue setup"
      variant="highlight"
      @action="onboarding.showModal.value = true"
    />

    <!-- Dashboard content -->
    <template v-if="workspaceStore.currentWorkspace">
      <!-- Greeting -->
      <ClientOnly>
        <p class="text-lg font-semibold text-neutral-900 dark:text-neutral-100">
          {{ greeting }}
        </p>
      </ClientOnly>

      <!-- Stat cards -->
      <StatStrip :columns="4">
        <StatTile
          data-guide="stats"
          icon="i-lucide-activity"
          :label="$t('dashboard.stats.running')"
          :value="dashboardStats?.running"
          :hint="dashboardStats?.queued ? $t('dashboard.stats.queued', { count: dashboardStats.queued }) : $t('dashboard.stats.nothingQueued')"
          :live="!!dashboardStats?.running"
          :loading="statsLoading"
        />
        <StatTile
          data-guide="stats"
          icon="i-lucide-circle-check"
          :label="$t('dashboard.stats.successfulRuns')"
          :value="dashboardStats?.successful_runs"
          :hint="windowHint"
          :loading="statsLoading"
        />
        <StatTile
          data-guide="stats"
          icon="i-lucide-git-pull-request"
          :label="$t('dashboard.stats.reviewedPrs')"
          :value="dashboardStats?.reviewed_prs"
          :hint="windowHint"
          :loading="statsLoading"
        />
        <TopUserTile
          data-guide="topUsers"
          :users="dashboardStats?.top_users ?? []"
          :loading="statsLoading"
        />
      </StatStrip>

      <!-- Active executions -->
      <div data-guide="live">
        <h2 class="text-sm font-semibold text-neutral-900 dark:text-neutral-100 mb-3">
          {{ $t('dashboard.activeExecutions.title') }}
        </h2>

        <div
          v-if="activeLoading"
          class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3"
        >
          <div
            v-for="i in 3"
            :key="i"
            class="h-28 rounded-xl border border-neutral-200 dark:border-neutral-700 bg-neutral-50 dark:bg-neutral-800/40 animate-pulse"
          />
        </div>

        <TransitionGroup
          v-else-if="activeExecutions.length"
          tag="div"
          name="exec-card"
          class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3"
        >
          <ActiveExecutionCard
            v-for="execution in activeExecutions"
            :key="execution.id"
            :execution="execution"
            :cancelling="cancellingExecutionId === execution.id"
            @open="openDetail(execution)"
            @cancel="handleCancel"
          />
        </TransitionGroup>

        <div
          v-else
          class="flex flex-col items-center justify-center rounded-xl border border-dashed border-neutral-200 dark:border-neutral-700 py-10"
        >
          <div class="size-12 flex items-center justify-center rounded-2xl bg-neutral-100 dark:bg-neutral-800 mb-3">
            <UIcon
              name="i-lucide-inbox"
              class="size-6 text-neutral-400 dark:text-neutral-500"
            />
          </div>
          <p class="text-sm font-medium text-neutral-700 dark:text-neutral-200">
            {{ $t('dashboard.activeExecutions.empty') }}
          </p>
          <p class="text-xs text-neutral-400 dark:text-neutral-500 mt-1 max-w-sm text-center">
            {{ $t('dashboard.activeExecutions.emptyDescription') }}
          </p>
        </div>
      </div>
    </template>

    <WorkspaceCreateModal v-model:open="showCreateModal" />

    <ExecutionResultModal
      v-if="selectedExecution"
      v-model:open="detailModalOpen"
      :title="selectedExecution.issue_title"
      :subtitle="selectedExecution.repo_name"
      :source="selectedExecution.source"
      :status="selectedExecution.status"
      :workflow="selectedExecution.workflow"
      :prompt="selectedExecution.prompt_text"
      :error-type="selectedExecution.error_type"
      :error-detail="selectedExecution.error_detail"
      :created-at="selectedExecution.started_at"
      :external-url="selectedExecution.pr_url"
      external-label="View pull request"
      :can-cancel="selectedExecution.status === 'running'"
      :cancelling="cancellingExecutionId === selectedExecution.id"
      @cancel="handleCancel(selectedExecution.id)"
    />
  </div>
</template>

<style scoped>
.exec-card-move,
.exec-card-enter-active,
.exec-card-leave-active {
  transition: opacity 0.3s ease, transform 0.3s ease;
}
.exec-card-enter-from {
  opacity: 0;
  transform: scale(0.96) translateY(4px);
}
.exec-card-leave-to {
  opacity: 0;
  transform: scale(0.94);
}
</style>

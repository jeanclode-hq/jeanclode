<script setup lang="ts">
import type { WorkspaceExecution } from '~/types/api'

const props = withDefaults(defineProps<{
  execution: WorkspaceExecution
  cancelling?: boolean
}>(), {
  cancelling: false,
})

const emit = defineEmits<{
  open: []
  cancel: [executionId: string]
}>()

const providerIconInfo = computed(() => getProviderIcon(props.execution.source))
const workflowLabel = computed(() => getWorkflowLabel(props.execution.workflow))
const workflowIcon = computed(() => getWorkflowIcon(props.execution.workflow))
const workflowColor = computed(() => getWorkflowColor(props.execution.workflow))
const workflowBg = computed(() => getWorkflowBg(props.execution.workflow))
const buttonLabel = computed(() => getExecStatusLabel(props.execution.status, props.execution.workflow))

const kindIcon = computed(() => props.execution.kind === 'issue' ? 'i-lucide-circle-dot' : 'i-lucide-git-pull-request')
const kindLabel = computed(() => {
  if (props.execution.kind === 'pull_request') {
    return props.execution.pr_number ? `PR #${props.execution.pr_number}` : 'Pull request'
  }
  return 'Issue'
})

function handleCancel() {
  emit('cancel', props.execution.id)
}
</script>

<template>
  <div
    class="flex flex-col gap-3 rounded-xl border border-neutral-200 dark:border-neutral-700 bg-white dark:bg-neutral-800 p-3.5 cursor-pointer shadow-sm hover:shadow-md hover:-translate-y-0.5 transition-all duration-200"
    @click="emit('open')"
  >
    <div class="flex items-center gap-2.5">
      <div class="flex items-center justify-center size-7 rounded-lg bg-neutral-100 dark:bg-neutral-700/60 shrink-0">
        <ProviderIcon
          v-bind="providerIconInfo"
          class="size-3.5 text-neutral-500 dark:text-neutral-400"
        />
      </div>
      <span class="text-xs text-neutral-400 dark:text-neutral-500 truncate flex-1">
        {{ execution.repo_name ?? execution.source_name }}
      </span>
      <span
        class="flex items-center gap-1 rounded-full pl-1.5 pr-2 py-0.5 text-[11px] font-medium shrink-0"
        :class="[workflowColor, workflowBg]"
      >
        <UIcon
          :name="workflowIcon"
          class="size-3"
        />
        {{ workflowLabel }}
      </span>
    </div>

    <p class="text-sm font-medium text-neutral-900 dark:text-neutral-100 line-clamp-2 flex-1">
      {{ execution.issue_title }}
    </p>

    <div class="flex items-center justify-between gap-2">
      <span class="flex items-center gap-1.5 text-xs text-neutral-400 dark:text-neutral-500 min-w-0">
        <UIcon
          :name="kindIcon"
          class="size-3.5 shrink-0"
        />
        <span class="truncate">{{ kindLabel }}</span>
        <span aria-hidden="true">·</span>
        <span class="shrink-0">{{ timeAgo(execution.started_at) }}</span>
      </span>
      <div
        class="shrink-0"
        @click.stop
      >
        <ExecutionRowButton
          :status="(execution.status as 'queued' | 'running')"
          :label="buttonLabel"
          :cancelling="cancelling"
          @cancel="handleCancel"
        />
      </div>
    </div>
  </div>
</template>

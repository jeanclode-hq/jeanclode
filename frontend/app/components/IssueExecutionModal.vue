<script setup lang="ts">
import type { Issue } from '~/types/api'

const props = withDefaults(defineProps<{
  issue: Issue | null
  retrying?: boolean
  // Mirrors the row's own FIX-button gating (isActionable in issues.vue) —
  // don't offer a retry path here that isn't offered there either, e.g.
  // once the underlying source issue is resolved/closed.
  retryable?: boolean
  cancelling?: boolean
}>(), {
  retrying: false,
  retryable: true,
  cancelling: false,
})

const emit = defineEmits<{
  retry: [issue: Issue]
  cancel: [executionId: string]
}>()

const open = defineModel<boolean>('open', { default: false })

// Reason/error text isn't in the issues list payload — only fetch the
// detail endpoint once the modal is actually open.
const issueId = computed(() => props.issue?.id ?? '')
const { data: detail, status: queryStatus } = useIssueQuery(
  issueId,
  computed(() => open.value && !!props.issue),
)

const loading = computed(() => open.value && queryStatus.value === 'pending')
const result = computed(() => detail.value?.result ?? props.issue?.result ?? null)
const triage = computed(() => detail.value?.triage_metadata ?? null)
const latestExecution = computed(() => detail.value?.executions?.[0] ?? null)
const subtitle = computed(() => props.issue?.project ?? null)
const externalLabel = computed(() => {
  const source = props.issue?.source ?? ''
  return `View in ${source.charAt(0).toUpperCase()}${source.slice(1)}`
})

// RUNNING only — a QUEUED execution has no container yet, and cancelling it
// would race the consumer that's about to dispatch it (see the backend's
// executions/route.py).
const canCancel = computed(() => {
  return !!latestExecution.value?.id && props.issue?.execution_status === 'running'
})

function handleRetry() {
  if (props.issue) emit('retry', props.issue)
}

function handleCancel() {
  if (latestExecution.value?.id) emit('cancel', latestExecution.value.id)
}
</script>

<template>
  <ExecutionResultModal
    v-if="issue"
    v-model:open="open"
    :title="issue.title"
    :subtitle="subtitle"
    :source="issue.source"
    :status="issue.execution_status"
    :workflow="issue.workflow ?? undefined"
    :result="result"
    :reason="triage?.reason"
    :root-cause="triage?.root_cause_hypothesis"
    :confidence="triage?.confidence ?? null"
    :affected-files="triage?.affected_files ?? []"
    :existing-pr-url="triage?.existing_pr_url"
    :previously-attempted="triage?.previously_attempted ?? false"
    :pull-requests="latestExecution?.pull_requests ?? []"
    :error-type="latestExecution?.error_type"
    :error-detail="latestExecution?.error_detail"
    :trigger="latestExecution?.trigger"
    :created-at="latestExecution?.created_at"
    :fixer-model="latestExecution?.fixer_llm_model"
    :fixer-credential="latestExecution?.fixer_llm_credential"
    :fixer-reason="latestExecution?.fixer_llm_reason"
    :external-url="issue.issue_url"
    :external-label="externalLabel"
    :loading="loading"
    :can-retry="issue.execution_status === 'failed' && retryable"
    :retrying="retrying"
    :can-cancel="canCancel"
    :cancelling="cancelling"
    @retry="handleRetry"
    @cancel="handleCancel"
  />
</template>

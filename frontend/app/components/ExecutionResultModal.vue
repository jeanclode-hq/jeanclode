<script setup lang="ts">
import type { ExecutionWorkflow, PullRequestSummary } from '~/types/api'

const props = withDefaults(defineProps<{
  title: string
  subtitle?: string | null
  // Provider the row belongs to (sentry/github/gitlab) — resolves the logo
  // shown in the header.
  source?: string | null

  // Raw execution status (infra-level: did it run without error).
  status: string
  workflow?: ExecutionWorkflow
  // Computed outcome (fixed / not actionable / PR open, etc.) — surfaced
  // only as a small leading icon next to the trigger info, not as text.
  result?: string | null

  // The @jeanclode-bot mention/comment text that triggered a RESPOND execution.
  prompt?: string | null

  // Triage reasoning (issues only).
  reason?: string | null
  rootCause?: string | null
  confidence?: number | null
  affectedFiles?: string[]
  existingPrUrl?: string | null
  previouslyAttempted?: boolean
  pullRequests?: PullRequestSummary[]

  // Failure detail.
  errorType?: string | null
  errorDetail?: string | null

  // Execution metadata.
  trigger?: string | null
  createdAt?: string | null
  // What an issue-resolve fixer ran on, and why triage picked it.
  fixerModel?: string | null
  fixerCredential?: string | null
  fixerReason?: string | null

  // Footer actions.
  externalUrl?: string | null
  externalLabel?: string | null
  loading?: boolean
  canRetry?: boolean
  retrying?: boolean
  canCancel?: boolean
  cancelling?: boolean
}>(), {
  subtitle: null,
  source: null,
  workflow: undefined,
  result: null,
  prompt: null,
  reason: null,
  rootCause: null,
  confidence: null,
  affectedFiles: () => [],
  existingPrUrl: null,
  previouslyAttempted: false,
  pullRequests: () => [],
  errorType: null,
  errorDetail: null,
  trigger: null,
  createdAt: null,
  fixerModel: null,
  fixerCredential: null,
  fixerReason: null,
  externalUrl: null,
  externalLabel: null,
  loading: false,
  canRetry: false,
  retrying: false,
  canCancel: false,
  cancelling: false,
})

const emit = defineEmits<{
  retry: []
  cancel: []
}>()

const open = defineModel<boolean>('open', { default: false })
const toast = useToast()

// Tailwind's scanner needs literal class strings — it can't see through a
// template-literal interpolation like `text-${color}-500`, so the full class
// name is spelled out here instead.
const RESULT_ICON_CLASSES: Record<string, string> = {
  success: 'text-success-500',
  error: 'text-error-500',
  info: 'text-info-500',
  warning: 'text-warning-500',
  neutral: 'text-neutral-400 dark:text-neutral-500',
  primary: 'text-primary-500',
}

const resultStatus = computed(() => props.result ?? props.status)
const resultIcon = computed(() => getExecStatusIcon(resultStatus.value))
const resultIconClass = computed(
  () => RESULT_ICON_CLASSES[getExecStatusColor(resultStatus.value)] ?? RESULT_ICON_CLASSES.neutral,
)
const resultLabel = computed(() => getExecStatusLabel(resultStatus.value, props.workflow ?? 'fix'))

const providerIconInfo = computed(() => getProviderIcon(props.source ?? ''))

const hasErrorDetail = computed(() => !!props.errorType || !!props.errorDetail)
const hasRootCause = computed(() => !!props.rootCause && props.rootCause !== props.reason)
const hasReasoning = computed(
  () => !!props.reason || hasRootCause.value || props.affectedFiles.length > 0,
)
const showExistingPr = computed(() => props.previouslyAttempted && !!props.existingPrUrl)
const isNothingYet = computed(
  () => !hasReasoning.value && !hasErrorDetail.value && !showExistingPr.value && !props.prompt && props.status === 'pending',
)

function handleRetry() {
  emit('retry')
  open.value = false
}

function handleCancel() {
  emit('cancel')
}

function openExternal(url: string) {
  navigateTo(url, { external: true, open: { target: '_blank' } })
}

async function copyError() {
  if (!props.errorDetail) return
  await navigator.clipboard.writeText(props.errorDetail)
  toast.add({ title: 'Copied to clipboard', color: 'success' })
}

const fixerTooltip = computed(() =>
  [props.fixerCredential, props.fixerReason || 'Default choice'].filter(Boolean).join(' — '),
)
</script>

<template>
  <UModal
    v-model:open="open"
    :title="title"
    :ui="{ content: 'sm:max-w-lg overflow-hidden' }"
  >
    <template #content="{ close }">
      <div class="flex items-start gap-3 px-5 pt-4 pb-2">
        <div class="flex items-center justify-center size-9 rounded-xl shrink-0 bg-neutral-100 dark:bg-neutral-800">
          <ProviderIcon
            :light-src="providerIconInfo.lightSrc"
            :dark-src="providerIconInfo.darkSrc"
            :fallback="providerIconInfo.fallback"
            class="size-4.5 text-neutral-500 dark:text-neutral-400"
          />
        </div>
        <div class="min-w-0 flex-1">
          <h2 class="text-[15px] font-semibold text-neutral-900 dark:text-neutral-100 leading-snug line-clamp-2">
            {{ title }}
          </h2>
          <p
            v-if="subtitle"
            class="text-xs text-neutral-400 dark:text-neutral-500 truncate mt-0.5"
          >
            {{ subtitle }}
          </p>
        </div>
        <UButton
          icon="i-lucide-x"
          size="sm"
          color="neutral"
          variant="ghost"
          class="shrink-0 -mt-1 -mr-1.5"
          aria-label="Close"
          @click="close"
        />
      </div>

      <div class="max-h-[70vh] overflow-y-auto px-5 pt-2 pb-4 space-y-4">
        <!-- Result + trigger -->
        <div class="flex items-center gap-4 text-sm text-neutral-500 dark:text-neutral-400">
          <span class="flex items-center gap-1.5">
            <UIcon
              :name="resultIcon"
              class="size-4"
              :class="resultIconClass"
            />
            {{ resultLabel }}
          </span>
          <span
            v-if="trigger"
            class="flex items-center gap-1.5"
          >
            <UIcon
              :name="trigger === 'manual' ? 'i-lucide-hand' : 'i-lucide-zap'"
              class="size-4"
            />
            <span class="capitalize">{{ trigger }}</span>
          </span>
          <span
            v-if="createdAt"
            class="flex items-center gap-1.5"
          >
            <UIcon
              name="i-lucide-clock"
              class="size-4"
            />
            {{ timeAgo(createdAt) }}
          </span>
          <UTooltip
            v-if="fixerModel"
            :text="fixerTooltip"
            :delay-duration="200"
          >
            <span class="flex items-center gap-1.5 min-w-0">
              <UIcon
                name="i-lucide-cpu"
                class="size-4 shrink-0"
              />
              <span class="truncate max-w-48">{{ fixerModel }}</span>
            </span>
          </UTooltip>
        </div>

        <template v-if="loading">
          <USkeleton class="h-4 w-full" />
          <USkeleton class="h-4 w-2/3" />
        </template>
        <template v-else>
          <!-- Prompt (RESPOND executions) -->
          <div
            v-if="prompt"
            class="flex items-start gap-2.5 rounded-lg bg-neutral-100 dark:bg-neutral-800 px-3 py-2.5"
          >
            <UIcon
              name="i-lucide-quote"
              class="size-4 shrink-0 mt-0.5 text-neutral-400 dark:text-neutral-500"
            />
            <p class="text-sm text-neutral-700 dark:text-neutral-300 leading-relaxed whitespace-pre-wrap">
              {{ prompt }}
            </p>
          </div>

          <!-- Existing PR notice -->
          <div
            v-if="showExistingPr"
            class="flex items-center gap-2.5 rounded-lg bg-info-50 dark:bg-info-950/30 px-3 py-2.5 text-sm text-info-700 dark:text-info-300"
          >
            <UIcon
              name="i-lucide-git-pull-request"
              class="size-4 shrink-0"
            />
            <span class="flex-1">A pull request already exists for this issue.</span>
            <UButton
              size="xs"
              color="info"
              variant="soft"
              trailing-icon="i-lucide-arrow-up-right"
              @click="openExternal(existingPrUrl!)"
            >
              View
            </UButton>
          </div>

          <!-- Reasoning -->
          <div
            v-if="hasReasoning"
            class="space-y-3"
          >
            <div v-if="reason">
              <p class="text-xs font-bold uppercase tracking-wide text-neutral-500 dark:text-neutral-400 mb-1">
                Assessment
              </p>
              <p class="text-sm text-neutral-700 dark:text-neutral-300 leading-relaxed">
                {{ reason }}
              </p>
            </div>

            <div v-if="hasRootCause">
              <p class="text-xs font-bold uppercase tracking-wide text-neutral-500 dark:text-neutral-400 mb-1">
                Root cause
              </p>
              <p class="text-sm text-neutral-700 dark:text-neutral-300 leading-relaxed">
                {{ rootCause }}
              </p>
            </div>

            <div
              v-if="affectedFiles.length"
              class="flex items-center gap-1.5 flex-wrap"
            >
              <span
                v-for="file in affectedFiles"
                :key="file"
                class="inline-flex items-center gap-1 text-xs font-mono bg-neutral-100 dark:bg-neutral-800 text-neutral-600 dark:text-neutral-300 rounded px-1.5 py-0.5"
              >
                <UIcon
                  name="i-lucide-file-code"
                  class="size-3"
                />
                {{ file }}
              </span>
            </div>
          </div>

          <!-- Error -->
          <div
            v-if="hasErrorDetail"
            class="rounded-lg border border-red-200 dark:border-red-900/40 overflow-hidden"
          >
            <div class="flex items-center gap-2 px-3 py-2 bg-red-50 dark:bg-red-950/30">
              <UIcon
                name="i-lucide-triangle-alert"
                class="size-4 text-red-500 shrink-0"
              />
              <p class="text-sm font-medium text-red-700 dark:text-red-300 truncate">
                {{ errorType || 'Unexpected error' }}
              </p>
              <UButton
                v-if="errorDetail"
                icon="i-lucide-copy"
                size="xs"
                color="neutral"
                variant="ghost"
                class="ml-auto shrink-0"
                aria-label="Copy error detail"
                @click="copyError"
              />
            </div>
            <pre
              v-if="errorDetail"
              class="text-xs font-mono text-neutral-700 dark:text-neutral-300 p-3 overflow-x-auto whitespace-pre-wrap max-h-56"
            >{{ errorDetail }}</pre>
          </div>

          <p
            v-if="isNothingYet"
            class="flex items-center gap-2 text-sm text-neutral-500 dark:text-neutral-400"
          >
            <UIcon
              name="i-lucide-clock"
              class="size-4 shrink-0"
            />
            No execution has run yet.
          </p>

          <!-- Pull/merge requests opened by this execution -->
          <div
            v-if="pullRequests.length"
            class="space-y-1 border-t border-neutral-200 dark:border-neutral-700 pt-3"
          >
            <button
              v-for="pr in pullRequests"
              :key="pr.id"
              type="button"
              class="flex items-center gap-2 w-full text-left text-sm text-neutral-600 dark:text-neutral-300 hover:text-primary-500 dark:hover:text-primary-400 transition-colors"
              @click="openExternal(pr.pr_url)"
            >
              <UIcon
                name="i-lucide-git-pull-request"
                class="size-3.5 shrink-0"
                :class="RESULT_ICON_CLASSES[getSourceStatusColor(pr.state)] ?? RESULT_ICON_CLASSES.neutral"
              />
              <span class="truncate flex-1">!{{ pr.pr_number }} {{ pr.title }}</span>
              <UIcon
                name="i-lucide-arrow-up-right"
                class="size-3.5 shrink-0 text-neutral-400"
              />
            </button>
          </div>

          <!-- Status + confidence, with the external link / retry action -->
          <div
            class="flex items-center justify-between gap-2 flex-wrap pt-3"
            :class="{ 'border-t border-neutral-200 dark:border-neutral-700': !pullRequests.length }"
          >
            <div class="flex items-center gap-1.5 flex-wrap">
              <ExecutionBadge
                :status="status"
                :workflow="workflow"
              />
              <UBadge
                v-if="confidence !== null"
                color="neutral"
                variant="subtle"
                size="sm"
              >
                {{ Math.round(confidence * 100) }}% confidence
              </UBadge>
            </div>
            <div
              v-if="externalUrl || canRetry || canCancel"
              class="flex items-center gap-2"
            >
              <UButton
                v-if="externalUrl"
                size="xs"
                color="neutral"
                variant="ghost"
                trailing-icon="i-lucide-arrow-up-right"
                @click="openExternal(externalUrl)"
              >
                {{ externalLabel || 'View source' }}
              </UButton>
              <AppButton
                v-if="canCancel"
                color="error"
                variant="soft"
                :processing="cancelling"
                icon="i-lucide-circle-x"
                @click="handleCancel"
              >
                {{ $t('common.cancel') }}
              </AppButton>
              <AppButton
                v-if="canRetry"
                :processing="retrying"
                icon="i-lucide-refresh-cw"
                @click="handleRetry"
              >
                Retry
              </AppButton>
            </div>
          </div>
        </template>
      </div>
    </template>
  </UModal>
</template>

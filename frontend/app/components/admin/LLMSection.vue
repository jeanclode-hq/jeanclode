<script setup lang="ts">
/**
 * LLM admin section — the priority-ordered credential pool (ADR-010).
 *
 * Each credential is tried in priority order; dispatch automatically fails
 * over to the next non-stale one. The parent page owns the actual API
 * calls and passes down the current list plus busy-state flags.
 */
import type { LLMCredentialInput, LLMCredentialView } from '~/composables/useAdmin'

const props = defineProps<{
  credentials: LLMCredentialView[]
  submitting?: boolean
  deletingId?: string | null
  reordering?: boolean
}>()

const emit = defineEmits<{
  create: [config: LLMCredentialInput]
  update: [id: string, config: LLMCredentialInput]
  delete: [id: string]
  reorder: [orderedIds: string[]]
}>()

const { t } = useI18n()

function providerLabel(provider?: string | null): string {
  switch (provider) {
    case 'claude_code': return 'Claude Code (subscription)'
    case 'anthropic': return 'Anthropic API'
    case 'openai': return 'OpenAI'
    case 'openai_compatible': return 'OpenAI-compatible'
    default: return provider || 'LLM'
  }
}

function statusLabel(credential: LLMCredentialView): string {
  if (credential.status !== 'stale') return t('admin.llm.active')
  if (!credential.stale_until) return t('admin.llm.stale')
  const when = new Date(credential.stale_until)
  return t('admin.llm.staleUntil', { when: when.toLocaleString() })
}

const sorted = computed(() => [...props.credentials].sort((a, b) => a.priority - b.priority))

const editingId = ref<string | null>(null)
const addingNew = ref(false)

function startEdit(id: string) {
  addingNew.value = false
  editingId.value = id
}

function cancelEdit() {
  editingId.value = null
}

function onCreate(config: LLMCredentialInput) {
  emit('create', config)
  addingNew.value = false
}

function onUpdate(id: string, config: LLMCredentialInput) {
  emit('update', id, config)
  editingId.value = null
}

function move(index: number, direction: -1 | 1) {
  const next = [...sorted.value]
  const target = index + direction
  if (target < 0 || target >= next.length) return
  const [item] = next.splice(index, 1)
  if (!item) return
  next.splice(target, 0, item)
  emit('reorder', next.map((c) => c.id))
}
</script>

<template>
  <SectionShell
    :title="t('admin.llm.heading')"
    :description="t('admin.llm.description')"
  >
    <div
      v-if="sorted.length"
      class="flex flex-col gap-3"
    >
      <div
        v-for="(credential, index) in sorted"
        :key="credential.id"
        class="rounded-lg border border-neutral-200 dark:border-neutral-700 overflow-hidden"
      >
        <template v-if="editingId === credential.id">
          <div class="p-4">
            <LLMForm
              :existing="credential"
              :submitting="submitting"
              @submit="(c) => onUpdate(credential.id, c)"
              @cancel="cancelEdit"
            />
          </div>
        </template>
        <template v-else>
          <div class="flex items-center gap-3 p-4 bg-neutral-50 dark:bg-neutral-800/40">
            <div class="flex flex-col shrink-0">
              <UButton
                icon="i-lucide-chevron-up"
                size="xs"
                variant="ghost"
                color="neutral"
                :disabled="index === 0 || reordering"
                @click="move(index, -1)"
              />
              <UButton
                icon="i-lucide-chevron-down"
                size="xs"
                variant="ghost"
                color="neutral"
                :disabled="index === sorted.length - 1 || reordering"
                @click="move(index, 1)"
              />
            </div>
            <div class="size-10 rounded-md flex items-center justify-center shrink-0 bg-neutral-800">
              <UIcon
                name="i-lucide-sparkles"
                class="size-5 text-white"
              />
            </div>
            <div class="flex-1 min-w-0">
              <p class="font-medium text-neutral-900 dark:text-neutral-100 truncate">
                {{ t('admin.llm.priority', { n: credential.priority }) }} — {{ credential.name || providerLabel(credential.provider) }}
              </p>
              <p class="text-xs text-neutral-500 flex items-center gap-1.5">
                <span
                  class="size-1.5 rounded-full"
                  :class="credential.status === 'stale' ? 'bg-amber-500' : 'bg-emerald-500'"
                />
                {{ statusLabel(credential) }}
                <span class="text-neutral-300 dark:text-neutral-600">·</span>
                {{ credential.model_high }}
                <template v-if="credential.model_heavy">
                  <span class="text-neutral-300 dark:text-neutral-600">·</span>
                  {{ t('admin.llm.heavyShort', { model: credential.model_heavy }) }}
                </template>
              </p>
            </div>
            <UButton
              icon="i-lucide-pencil"
              size="sm"
              variant="ghost"
              color="neutral"
              @click="startEdit(credential.id)"
            />
          </div>
          <div class="p-2 flex justify-end">
            <DeleteProviderAction
              :button-label="t('admin.llm.remove')"
              :confirm-message="t('admin.llm.confirmRemove')"
              :confirm-button-label="t('admin.confirmDeleteButton')"
              :cancel-label="t('admin.cancel')"
              :loading="deletingId === credential.id"
              @confirm="emit('delete', credential.id)"
            />
          </div>
        </template>
      </div>
    </div>

    <div
      v-if="addingNew"
      class="mt-4 rounded-lg border border-neutral-200 dark:border-neutral-700 p-4"
    >
      <LLMForm
        :existing="null"
        :submitting="submitting"
        @submit="onCreate"
        @cancel="addingNew = false"
      />
    </div>
    <UButton
      v-else
      class="mt-4"
      variant="outline"
      color="neutral"
      icon="i-lucide-plus"
      @click="addingNew = true; editingId = null"
    >
      {{ t('admin.llm.add') }}
    </UButton>
  </SectionShell>
</template>

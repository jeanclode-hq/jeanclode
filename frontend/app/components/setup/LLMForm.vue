<script setup lang="ts">
/**
 * Form for adding or editing one credential in the LLM pool (ADR-010).
 *
 * Each provider exposes a curated list of models via dropdown. Opus is
 * intentionally excluded for Claude providers — it's too expensive as a
 * default for the automated workflows. ``openai_compatible`` uses free-text
 * inputs since the model list depends on the user's endpoint. ``kind`` is
 * derived from ``provider`` rather than its own control — ``claude_code``
 * is always an OAuth subscription, everything else is an API key.
 * ``model_heavy`` is opt-in: left empty, triage never escalates the fixer.
 */
import type { LLMCredentialInput, LLMCredentialView } from '~/composables/useAdmin'

const props = defineProps<{
  existing?: LLMCredentialView | null
  submitting?: boolean
}>()

const emit = defineEmits<{
  submit: [config: LLMCredentialInput]
  cancel: []
}>()

const { t } = useI18n()

type ProviderId = LLMCredentialInput['provider']

const providerOptions = [
  { label: 'Claude Code (subscription)', value: 'claude_code' as const },
  { label: 'Anthropic API', value: 'anthropic' as const },
  { label: 'OpenAI', value: 'openai' as const },
  { label: 'OpenAI-compatible', value: 'openai_compatible' as const },
]

const planTierOptions = [
  { label: 'Pro', value: 'pro' as const },
  { label: 'Max', value: 'max' as const },
  { label: 'Max 5x', value: 'max_5x' as const },
]

const claudeModels = [
  { label: 'Claude Opus 4.6', value: 'claude-opus-4-6' },
  { label: 'Claude Sonnet 5', value: 'claude-sonnet-5' },
  { label: 'Claude Haiku 4.5', value: 'claude-haiku-4-5' },
]

const openaiModels = [
  { label: 'GPT-5.4', value: 'gpt-5.4' },
  { label: 'GPT-5.4 mini', value: 'gpt-5.4-mini' },
  { label: 'GPT-5.4 nano', value: 'gpt-5.4-nano' },
]

const providerDefaults: Record<ProviderId, { high: string, low: string }> = {
  claude_code: { high: 'claude-sonnet-5', low: 'claude-haiku-4-5' },
  anthropic: { high: 'claude-sonnet-5', low: 'claude-haiku-4-5' },
  openai: { high: 'gpt-5.4', low: 'gpt-5.4-mini' },
  openai_compatible: { high: '', low: '' },
}

// Reka's SelectItem rejects an empty-string value, so "no heavy model" needs a sentinel.
const NO_HEAVY = '__none__'

const initialProvider = (props.existing?.provider as ProviderId | undefined) ?? 'claude_code'
const defaults = providerDefaults[initialProvider]

const form = reactive({
  provider: initialProvider,
  secret: '',
  name: props.existing?.name ?? '',
  model_high: props.existing?.model_high || defaults.high,
  model_heavy: props.existing?.model_heavy ?? '',
  model_low: props.existing?.model_low || defaults.low,
  base_url: props.existing?.base_url ?? null as string | null,
  plan_tier: (props.existing?.plan_tier ?? null) as LLMCredentialInput['plan_tier'],
})

const isClaude = computed(() => form.provider === 'claude_code' || form.provider === 'anthropic')
const isOpenAI = computed(() => form.provider === 'openai')
const isCompatible = computed(() => form.provider === 'openai_compatible')

const modelOptions = computed(() => {
  if (isClaude.value) return claudeModels
  if (isOpenAI.value) return openaiModels
  return []
})

const heavyOptions = computed(() => [
  { label: t('admin.llm.modelHeavyNone'), value: NO_HEAVY },
  ...modelOptions.value,
])

watch(() => form.provider, (next) => {
  const d = providerDefaults[next]
  form.model_high = d.high
  form.model_low = d.low
  form.model_heavy = ''
  if (next !== 'openai_compatible') {
    form.base_url = null
  }
  if (next !== 'claude_code') {
    form.plan_tier = null
  }
})

function onSubmit() {
  const payload: LLMCredentialInput = {
    kind: form.provider === 'claude_code' ? 'oauth_subscription' : 'api_key',
    provider: form.provider,
    secret: form.secret,
    name: form.name.trim(),
    model_high: form.model_high,
    model_heavy: form.model_heavy.trim(),
    model_low: form.model_low,
    base_url: form.provider === 'openai_compatible' ? form.base_url : null,
    plan_tier: form.provider === 'claude_code' ? form.plan_tier : null,
  }
  emit('submit', payload)
}
</script>

<template>
  <form
    class="flex flex-col gap-4"
    @submit.prevent="onSubmit"
  >
    <UFormField
      :label="$t('admin.llm.provider')"
      name="provider"
      required
    >
      <USelect
        v-model="form.provider"
        :items="providerOptions"
        class="w-full"
      />
    </UFormField>

    <UFormField
      :label="$t('admin.llm.name')"
      name="name"
      :help="$t('admin.llm.nameHelp')"
    >
      <UInput
        v-model="form.name"
        maxlength="100"
        :placeholder="form.provider"
        class="w-full"
      />
    </UFormField>

    <UFormField
      :label="form.provider === 'claude_code' ? $t('admin.llm.oauthToken') : $t('admin.llm.apiKey')"
      name="secret"
      :help="existing?.secret
        ? $t('admin.leaveBlankToKeep')
        : (form.provider === 'claude_code' ? $t('admin.llm.oauthTokenHelp') : undefined)"
    >
      <UInput
        v-model="form.secret"
        type="password"
        placeholder="••••••••"
        class="w-full"
        :required="!existing?.secret"
      />
    </UFormField>

    <UFormField
      v-if="form.provider === 'claude_code'"
      :label="$t('admin.llm.planTier')"
      name="plan_tier"
      :help="$t('admin.llm.planTierHelp')"
    >
      <USelect
        :model-value="form.plan_tier ?? undefined"
        :items="planTierOptions"
        class="w-full"
        @update:model-value="form.plan_tier = ($event as typeof form.plan_tier) ?? null"
      />
    </UFormField>

    <UFormField
      :label="$t('admin.llm.modelHigh')"
      name="model_high"
      :help="$t('admin.llm.modelHighHelp')"
      required
    >
      <USelect
        v-if="!isCompatible"
        v-model="form.model_high"
        :items="modelOptions"
        class="w-full"
      />
      <UInput
        v-else
        v-model="form.model_high"
        class="w-full"
        required
      />
    </UFormField>

    <UFormField
      :label="$t('admin.llm.modelHeavy')"
      name="model_heavy"
      :help="$t('admin.llm.modelHeavyHelp')"
    >
      <USelect
        v-if="!isCompatible"
        :model-value="form.model_heavy || NO_HEAVY"
        :items="heavyOptions"
        class="w-full"
        @update:model-value="form.model_heavy = $event === NO_HEAVY ? '' : ($event as string)"
      />
      <UInput
        v-else
        v-model="form.model_heavy"
        class="w-full"
      />
    </UFormField>

    <UFormField
      :label="$t('admin.llm.modelLow')"
      name="model_low"
      :help="$t('admin.llm.modelLowHelp')"
      required
    >
      <USelect
        v-if="!isCompatible"
        v-model="form.model_low"
        :items="modelOptions"
        class="w-full"
      />
      <UInput
        v-else
        v-model="form.model_low"
        class="w-full"
        required
      />
    </UFormField>

    <UFormField
      v-if="isCompatible"
      :label="$t('admin.llm.baseUrl')"
      name="base_url"
      required
    >
      <UInput
        :model-value="form.base_url ?? ''"
        placeholder="https://your-endpoint.example.com/v1"
        class="w-full"
        required
        @update:model-value="form.base_url = ($event as string) || null"
      />
    </UFormField>

    <div class="flex gap-2">
      <UButton
        type="submit"
        :loading="submitting"
        class="flex-1"
        block
      >
        {{ $t('admin.save') }}
      </UButton>
      <UButton
        v-if="existing"
        type="button"
        variant="ghost"
        color="neutral"
        @click="emit('cancel')"
      >
        {{ $t('admin.cancel') }}
      </UButton>
    </div>
  </form>
</template>

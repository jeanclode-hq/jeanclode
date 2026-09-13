<script setup lang="ts">
/**
 * Form for entering or updating GitLab OAuth credentials.
 */
import type { GitLabConfigInput, GitLabConfigView } from '~/composables/useAdmin'

const props = defineProps<{
  existing?: GitLabConfigView | null
  submitting?: boolean
}>()

const emit = defineEmits<{
  submit: [config: GitLabConfigInput]
}>()

const form = reactive<GitLabConfigInput>({
  client_id: props.existing?.client_id ?? '',
  client_secret: '',
  instance_url: props.existing?.instance_url ?? 'https://gitlab.com',
  webhook_secret: '',
})

function onSubmit() {
  emit('submit', { ...form })
}
</script>

<template>
  <form
    class="flex flex-col gap-4"
    @submit.prevent="onSubmit"
  >
    <UFormField
      :label="$t('admin.gitlab.instanceUrl')"
      name="instance_url"
      required
    >
      <UInput
        v-model="form.instance_url"
        placeholder="https://gitlab.com"
        class="w-full"
      />
    </UFormField>

    <UFormField
      :label="$t('admin.gitlab.clientId')"
      name="client_id"
      required
    >
      <UInput
        v-model="form.client_id"
        class="w-full"
      />
    </UFormField>

    <UFormField
      :label="$t('admin.gitlab.clientSecret')"
      name="client_secret"
      :help="existing?.client_secret ? $t('admin.leaveBlankToKeep') : undefined"
    >
      <UInput
        v-model="form.client_secret"
        type="password"
        placeholder="••••••••"
        class="w-full"
        :required="!existing?.client_secret"
      />
    </UFormField>

    <UFormField
      :label="$t('admin.gitlab.webhookSecret')"
      name="webhook_secret"
      :help="existing?.webhook_secret
        ? $t('admin.leaveBlankToKeep')
        : $t('admin.gitlab.webhookSecretHelp')"
    >
      <UInput
        v-model="form.webhook_secret"
        type="password"
        placeholder="••••••••"
        class="w-full"
      />
    </UFormField>

    <UButton
      type="submit"
      :loading="submitting"
      block
    >
      {{ $t('admin.save') }}
    </UButton>
  </form>
</template>
